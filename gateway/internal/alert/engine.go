// Package alert 实现网关的内置告警引擎与通知渠道。
//
// 三条设计约束：
//  1. 只依赖 internal/model（数据契约）、internal/metrics（窗口聚合）和标准库——
//     不导入 proxy / provider，避免包循环，也让告警能在没有真实流量的情况下单测；
//  2. 「拉模型」评估：定时从 Aggregator 读窗口/瞬时指标 → 与阈值比较 → 产生
//     pending / firing / resolved 事件；状态全在内存（事件环形 200 条），重启即清空；
//  3. 通知渠道 = 接口 + 注册表（log / webhook / dingtalk / slack），新增渠道只需实现
//     Notifier 并调用 Register，不动引擎。
//
// 与外部 Prometheus 链路的分工：内置引擎负责「网关自己就能告警、零依赖部署」，
// Prometheus + Alertmanager 负责「长期留存、跨服务聚合」。两条链路的规则各自独立，
// 见 deploy/prometheus/alerts.yml 与 docs/alerts.md。
package alert

import (
	"errors"
	"fmt"
	"log"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"tsukiyohub/gateway/internal/metrics"
	"tsukiyohub/gateway/internal/model"
)

const (
	// defaultInterval 配置没写 interval_ms 时的评估间隔。
	defaultInterval = 15 * time.Second
	// minInterval 评估间隔下限，防止 interval_ms=1 把 CPU 打满。
	minInterval = 100 * time.Millisecond
	// defaultCooldown 配置没写 default_cooldown_seconds 时的通知冷却。
	defaultCooldown = 5 * time.Minute
	// eventCapacity 事件环形缓冲容量（契约：最近 200 条）。
	eventCapacity = 200
	// overviewRecent 概览里带最近几条事件。
	overviewRecent = 10
)

// Engine 内置告警引擎，实现 model.AlertEngine。
//
// 并发模型：规则状态、事件环、通知渠道都受 mu 保护；评估与通知发送在后台
// goroutine 里串行进行（保证事件状态与实际发送顺序一致）。
type Engine struct {
	agg    *metrics.Aggregator
	logger *log.Logger

	mu              sync.RWMutex
	enabled         bool
	gateway         string
	interval        time.Duration
	defaultCooldown time.Duration

	rules     map[string]*ruleState // rule id → 状态
	order     []string              // 配置顺序（管理接口按这个顺序展示）
	events    *eventRing
	notifiers []*notifierMount
	warned    map[string]bool // 一次性 warning（未知指标等）

	lastCheck time.Time
	// now 时间来源；生产恒为 time.Now，单测替换成假时钟来验证 for/cooldown/静默。
	now func() time.Time

	started bool
	stopCh  chan struct{}
	wg      sync.WaitGroup

	// persist 规则热替换后的持久化回调（由 cmd 注入 config.Save），nil = 只改内存。
	persist func([]model.AlertRule) error

	seq uint64 // 事件 id 自增序号
}

// 编译期断言：Engine 必须完整实现 model.AlertEngine。
var _ model.AlertEngine = (*Engine)(nil)

// New 构造告警引擎。
//
// cfg 用来取 alerts.enabled / interval_ms / default_cooldown_seconds / rules / notifiers
// 与 public_url；agg 提供窗口与瞬时指标；logger 为 nil 时退化为写 stderr 的默认 logger。
// 配置里的坏规则只记 warning 并跳过（引擎尽量启动），严格校验发生在 ReplaceRules。
func New(cfg *model.Config, agg *metrics.Aggregator, logger *log.Logger) *Engine {
	if logger == nil {
		logger = log.New(os.Stderr, "", log.LstdFlags)
	}
	if agg == nil {
		agg = metrics.NewAggregator()
	}
	e := &Engine{
		agg:             agg,
		logger:          logger,
		now:             time.Now,
		events:          newEventRing(eventCapacity),
		rules:           map[string]*ruleState{},
		warned:          map[string]bool{},
		interval:        defaultInterval,
		defaultCooldown: defaultCooldown,
	}
	if cfg != nil {
		e.applyMeta(cfg)
		compiled, errs := compileRules(cfg.Alerts.Rules)
		for _, err := range errs {
			logger.Printf("[alert] 规则配置有误，已跳过：%v", err)
		}
		if len(compiled) > 0 {
			e.mu.Lock()
			e.setRulesLocked(compiled, false)
			e.mu.Unlock()
		}
	}
	if len(e.notifiers) == 0 {
		logger.Printf("[alert] 未配置任何可用通知渠道：事件只会出现在 /admin/api/alerts/events")
	}
	return e
}

// applyMeta 把配置里与规则无关的部分（开关/间隔/冷却/渠道/网关地址）写进引擎。
// 调用方负责加锁（New 里尚未并发，直接调用）。
func (e *Engine) applyMeta(cfg *model.Config) {
	interval := defaultInterval
	if ms := cfg.Alerts.IntervalMS; ms > 0 {
		interval = time.Duration(ms) * time.Millisecond
	}
	if interval < minInterval {
		interval = minInterval
	}
	cooldown := defaultCooldown
	if s := cfg.Alerts.DefaultCooldownSeconds; s > 0 {
		cooldown = time.Duration(s) * time.Second
	}
	e.enabled = cfg.Alerts.Enabled
	e.gateway = cfg.PublicURL
	e.interval = interval
	e.defaultCooldown = cooldown
	e.notifiers = buildNotifiers(cfg.Alerts.Notifiers, e.logger)
}

// Start 启动后台评估循环；未启用（alerts.enabled=false）时直接返回。
//
// 幂等：重复调用只会启动一次。
func (e *Engine) Start() {
	e.mu.Lock()
	if e.started {
		e.mu.Unlock()
		return
	}
	if !e.enabled {
		e.mu.Unlock()
		e.logger.Printf("[alert] 告警引擎未启用（alerts.enabled=false），Start 被忽略")
		return
	}
	stopCh := make(chan struct{})
	e.stopCh = stopCh
	e.started = true
	interval, rules := e.interval, len(e.order)
	// 启动时把每条规则「编译后的样子」记进日志：排查「规则为什么没触发」时，
	// 第一眼看的就是 window/for/scope 有没有按预期解析。
	descriptions := make([]string, 0, len(e.order))
	for _, id := range e.order {
		if st := e.rules[id]; st != nil && st.rule != nil {
			descriptions = append(descriptions, formatRuleDebug(st.rule))
		}
	}
	e.mu.Unlock()

	e.wg.Add(1)
	go e.loop(stopCh)
	e.logger.Printf("[alert] 告警引擎已启动：%d 条规则，评估间隔 %s", rules, interval)
	for _, desc := range descriptions {
		e.logger.Printf("[alert] 规则 %s", desc)
	}
}

// Stop 停止评估循环并等待当前一轮结束；幂等。
func (e *Engine) Stop() {
	e.mu.Lock()
	if !e.started {
		e.mu.Unlock()
		return
	}
	e.started = false
	if e.stopCh != nil {
		close(e.stopCh)
		e.stopCh = nil
	}
	e.mu.Unlock()
	e.wg.Wait()
	e.logger.Printf("[alert] 告警引擎已停止")
}

// loop 后台评估循环。
//
// 每轮重新读 interval（而不是持有一个 ticker），这样 ApplyConfig 改间隔能自然生效。
func (e *Engine) loop(stopCh <-chan struct{}) {
	defer e.wg.Done()
	for {
		e.mu.RLock()
		interval := e.interval
		e.mu.RUnlock()

		timer := time.NewTimer(interval)
		select {
		case <-stopCh:
			timer.Stop()
			return
		case <-timer.C:
			e.EvaluateOnce()
		}
	}
}

// Overview 返回引擎概况；Recent 是最新的 10 条事件（最新在前）。
func (e *Engine) Overview() model.AlertOverview {
	e.mu.RLock()
	defer e.mu.RUnlock()

	ov := model.AlertOverview{
		Enabled:   e.enabled,
		Rules:     len(e.order),
		LastCheck: e.lastCheck,
	}
	for _, st := range e.rules {
		if st.event == nil {
			continue
		}
		switch st.event.Status {
		case statusFiring:
			ov.Firing++
		case statusPending:
			ov.Pending++
		}
	}
	ov.Recent = e.events.list(overviewRecent, "")
	return ov
}

// ListRules 返回当前规则（配置顺序，含未启用的规则）。
func (e *Engine) ListRules() []model.AlertRule {
	e.mu.RLock()
	defer e.mu.RUnlock()
	out := make([]model.AlertRule, 0, len(e.order))
	for _, id := range e.order {
		if st := e.rules[id]; st != nil && st.rule != nil {
			out = append(out, cloneRule(st.rule.rule))
		}
	}
	return out
}

// ReplaceRules 热替换规则。
//
// 语义（契约要求）：
//   - 全部规则先整体编译，**任何一条非法就整体拒绝**（原子替换，不留半套规则）；
//   - 未变化的规则保留运行状态（活动事件、for 计时、冷却、静默）与历史事件；
//   - 变更/新增的规则重建状态：旧的活动事件就地收尾为 resolved（不通知），
//     避免管理界面上永远挂着一条不会再更新的 firing；
//   - 被删除的规则，其历史事件从事件环里移除。
//
// 规则生效后调用 persist 回调（若注入）落盘；落盘失败会返回错误，但规则已在内存生效。
func (e *Engine) ReplaceRules(rules []model.AlertRule) error {
	compiled, errs := compileRules(rules)
	if len(errs) > 0 {
		return errors.Join(errs...)
	}

	e.mu.Lock()
	e.setRulesLocked(compiled, true)
	persist := e.persist
	snapshot := make([]model.AlertRule, 0, len(compiled))
	for _, cr := range compiled {
		snapshot = append(snapshot, cloneRule(cr.rule))
	}
	e.mu.Unlock()

	if persist != nil {
		if err := persist(snapshot); err != nil {
			return fmt.Errorf("规则已生效，但持久化失败：%w", err)
		}
	}
	return nil
}

// setRulesLocked 安装编译后的规则（调用方必须持写锁）。
//
// keepState=true 表示热替换：定义完全相同的规则复用旧状态；
// false 用于启动初始化。
func (e *Engine) setRulesLocked(compiled []*compiledRule, keepState bool) {
	now := e.now()
	next := make(map[string]*ruleState, len(compiled))
	order := make([]string, 0, len(compiled))

	for _, cr := range compiled {
		id := cr.rule.ID
		order = append(order, id)

		if keepState {
			if old := e.rules[id]; old != nil && old.rule != nil && reflect.DeepEqual(old.rule.rule, cr.rule) {
				next[id] = old
				continue
			}
		}
		// 规则定义变了（或新增）：旧活动事件就地收尾，不通知。
		if old := e.rules[id]; old != nil && old.event != nil {
			old.event.Status = statusResolved
			old.event.EndsAt = timePtr(now)
			old.event = nil
		}
		next[id] = &ruleState{rule: cr}
	}

	e.rules = next
	e.order = order
	// 已删除规则的历史事件不再有意义
	e.events.retain(func(ev *model.AlertEvent) bool { return next[ev.RuleID] != nil })
}

// ListEvents 返回最近事件（最新在前）；limit <= 0 = 50，status 为空 = 全部。
func (e *Engine) ListEvents(limit int, status string) []model.AlertEvent {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.events.list(limit, strings.ToLower(strings.TrimSpace(status)))
}

// Ack 确认一条事件：事件状态改为 acked，并在 duration 内不再发送该规则的通知。
//
// duration <= 0 时使用全局默认冷却（default_cooldown_seconds）。
// 已恢复的事件不允许确认（返回错误），避免界面误操作把历史清成 acked。
func (e *Engine) Ack(id, by string, duration time.Duration) error {
	if strings.TrimSpace(id) == "" {
		return errors.New("事件 id 不能为空")
	}
	e.mu.Lock()
	defer e.mu.Unlock()

	ev := e.events.get(id)
	if ev == nil {
		return fmt.Errorf("告警事件 %s 不存在", id)
	}
	if ev.Status == statusResolved {
		return fmt.Errorf("告警事件 %s 已恢复，无需确认", id)
	}
	now := e.now()
	if duration <= 0 {
		duration = e.defaultCooldown
	}
	until := now.Add(duration)
	by = strings.TrimSpace(by)
	if by == "" {
		by = "unknown"
	}

	ev.Status = statusAcked
	ev.AckedBy = by
	ev.AckedAt = timePtr(now)
	ev.SilencedUntil = timePtr(until)

	if st := e.rules[ev.RuleID]; st != nil {
		st.silencedUntil = until
		// 静默结束后的第一条通知仍受冷却约束，否则刚到期就立刻再响一次。
		st.lastNotified = now
		// 只有在没有更新的事件时才把该事件挂回规则状态。
		if st.event == nil {
			st.event = ev
		}
	}
	e.logger.Printf("[alert] 事件 %s（规则 %s）已被 %s 确认，静默至 %s",
		ev.ID, ev.RuleID, by, until.Format(time.RFC3339))
	return nil
}

// Notifiers 返回通知渠道展示视图（脱敏：不含 url / secret）。
func (e *Engine) Notifiers() []model.NotifierView {
	e.mu.RLock()
	defer e.mu.RUnlock()
	out := make([]model.NotifierView, 0, len(e.notifiers))
	for _, m := range e.notifiers {
		// 展示规范化后的级别（"warn" → "warning"）；空 = info（等于不过滤）。
		minSeverity := SeverityInfo
		if strings.TrimSpace(m.cfg.MinSeverity) != "" {
			if normalized, err := normalizeSeverity(m.cfg.MinSeverity); err == nil {
				minSeverity = normalized
			}
		}
		out = append(out, model.NotifierView{
			Name:        m.cfg.Name,
			Type:        strings.ToLower(strings.TrimSpace(m.cfg.Type)),
			Enabled:     m.cfg.Enabled,
			MinSeverity: minSeverity,
		})
	}
	return out
}

// ApplyConfig 用整份配置刷新引擎（开关/间隔/冷却/渠道/规则）。
//
// 这是 ReplaceRules 的超集：cmd 在配置热更新时调它就能让 notifier 变更也生效。
// 规则非法时不会替换规则，返回错误；其余字段照常生效。
func (e *Engine) ApplyConfig(cfg *model.Config) error {
	if cfg == nil {
		return errors.New("配置不能为空")
	}
	compiled, errs := compileRules(cfg.Alerts.Rules)

	e.mu.Lock()
	e.applyMeta(cfg)
	if len(errs) == 0 {
		e.setRulesLocked(compiled, true)
	}
	interval := e.interval
	e.mu.Unlock()

	if len(errs) > 0 {
		for _, err := range errs {
			e.logger.Printf("[alert] 配置热更新：规则有误，规则未被替换：%v", err)
		}
		return errors.Join(errs...)
	}
	e.logger.Printf("[alert] 配置热更新完成：%d 条规则，间隔 %s", len(compiled), interval)
	return nil
}

// SetPersist 注入规则持久化回调（通常由 cmd 传 config.Save 的包装）。
//
// 不注入时 ReplaceRules 只改内存——admin 接口的「保存规则」仍是当前进程有效。
func (e *Engine) SetPersist(fn func([]model.AlertRule) error) {
	e.mu.Lock()
	e.persist = fn
	e.mu.Unlock()
}

// newEventID 生成事件 id：规则 id + 毫秒时间 + 自增序号（同一毫秒内也不重复）。
func (e *Engine) newEventID(ruleID string, now time.Time) string {
	seq := atomic.AddUint64(&e.seq, 1)
	return fmt.Sprintf("%s-%d-%d", ruleID, now.UnixMilli(), seq)
}
