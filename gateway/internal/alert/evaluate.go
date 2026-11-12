package alert

// 本文件是「评估循环」：定时读取指标 → 与阈值比较 → 驱动事件状态机 → 产生通知。
//
// 状态机规则（严格按契约）：
//
//	条件满足 ∧ 无活动事件        → 新建 pending 事件，StartsAt = now
//	pending ∧ now-StartsAt ≥ For → 转 firing，发通知（For 为空/0 时当轮就 fired）
//	firing ∧ Cooldown 已过        → 重新发一条通知（持续告警的周期性提醒）
//	条件恢复                     → resolved；只有「已经通知过」的事件才发「已恢复」
//	Ack 后 Silence 期内           → 保持 acked，不通知、不迁移状态
//
// 时间来源统一用 e.now（生产 = time.Now，单测可替换），因此 for / cooldown / 静默
// 都能在毫秒级测试里验证，不需要真的 sleep。

import (
	"fmt"
	"strings"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// ruleState 一条规则的运行时状态（由 Engine.mu 保护）。
type ruleState struct {
	// rule 编译后的规则（热替换后指针会换，未变规则复用旧的）
	rule *compiledRule
	// event 当前活动事件（pending / firing / acked）；nil = 没有告警
	event *model.AlertEvent
	// lastNotified 上次发通知的时间（cooldown 基准，跨事件保留）
	lastNotified time.Time
	// silencedUntil Ack 后的静默截止时间
	silencedUntil time.Time
}

// outgoing 待发送的一条通知。
type outgoing struct {
	event model.AlertEvent
	rule  model.AlertRule
}

// EvaluateOnce 立即评估一轮所有启用规则。
//
// Start 的后台循环按 IntervalMS 调它；单测/排查也可以直接调用。
// 通知在锁外发送（HTTP 可能慢），这样管理接口在发送期间仍能读到事件状态。
func (e *Engine) EvaluateOnce() {
	e.mu.Lock()
	now := e.now()
	var outs []outgoing
	if e.enabled {
		e.lastCheck = now
		for _, id := range e.order {
			st := e.rules[id]
			if st == nil || st.rule == nil || !st.rule.rule.Enabled {
				continue
			}
			value, ok := e.resolveLocked(st.rule)
			if !ok {
				continue
			}
			if st.rule.compare(value) {
				outs = append(outs, e.onSatisfiedLocked(st, now, value)...)
			} else {
				outs = append(outs, e.onRecoveredLocked(st, now, value)...)
			}
		}
	}
	// 通知渠道与网关地址在锁内取快照：slice 本身不可变，锁外发送是安全的。
	mounts := e.notifiers
	gateway := e.gateway
	e.mu.Unlock()

	if len(outs) > 0 {
		e.dispatch(outs, mounts, gateway)
	}
}

// resolveLocked 取规则当前值。
//
// 未知指标（写错 metric 名）只跳过一次 warning：一个错字不该让整个引擎每 15 秒刷屏，
// 也不该阻断其它规则。
func (e *Engine) resolveLocked(cr *compiledRule) (float64, bool) {
	if cr.unknown {
		key := "metric:" + cr.metric
		if !e.warned[key] {
			e.warned[key] = true
			e.logger.Printf("[alert] 警告：规则 %s 使用了未知指标 %q，该规则将被跳过（支持：%s）",
				cr.rule.ID, cr.metric, strings.Join(SupportedMetrics(), ", "))
		}
		return 0, false
	}
	if cr.instant {
		return e.agg.Instant(cr.metric), true
	}
	snap := e.agg.Query(cr.windowSec, cr.scope)
	if cr.extract == nil {
		return 0, false
	}
	return cr.extract(snap), true
}

// onSatisfiedLocked 条件满足时的状态迁移。
func (e *Engine) onSatisfiedLocked(st *ruleState, now time.Time, value float64) []outgoing {
	cr := st.rule
	if st.event == nil {
		ev := &model.AlertEvent{
			ID:        e.newEventID(cr.rule.ID, now),
			RuleID:    cr.rule.ID,
			RuleName:  cr.rule.Name,
			Metric:    cr.metric,
			Scope:     cloneScope(cr.rule.Scope),
			Value:     value,
			Threshold: cr.threshold,
			Op:        cr.op,
			Severity:  cr.severity,
			Message:   cr.rule.Message,
			Status:    statusPending,
			StartsAt:  now,
		}
		e.events.add(ev)
		st.event = ev
	} else {
		// 同一事件持续中：只刷新指标值（通知里展示的是最新值）
		st.event.Value = value
	}

	ev := st.event
	// Ack 后的静默期：状态保持不变（acked），不发通知。
	if e.silenced(st, now) {
		return nil
	}

	switch ev.Status {
	case statusPending:
		if now.Sub(ev.StartsAt) < cr.forDur {
			return nil
		}
		return e.fireLocked(st, now)
	case statusAcked:
		// 静默到期且条件仍满足：回到 firing 并通知
		return e.fireLocked(st, now)
	case statusFiring:
		if e.cooldownElapsed(st, now) {
			return e.fireLocked(st, now)
		}
	}
	return nil
}

// onRecoveredLocked 条件恢复时的状态迁移。
func (e *Engine) onRecoveredLocked(st *ruleState, now time.Time, value float64) []outgoing {
	ev := st.event
	if ev == nil {
		return nil
	}
	// 只有真正通知过的事件才发「已恢复」：pending 阶段就恢复过来的告警用户根本不知道，
	// 再发一条恢复通知只会制造噪音。
	wasNotified := ev.Status == statusFiring || ev.Status == statusAcked
	silenced := e.silenced(st, now)

	ev.Status = statusResolved
	ev.Value = value
	ev.EndsAt = timePtr(now)
	st.event = nil
	// 静默是事件级的：事件结束就失效，下一条新告警不应继承它。
	st.silencedUntil = time.Time{}

	if !wasNotified || silenced {
		return nil
	}
	ev.Notified = true
	return []outgoing{{event: *ev, rule: st.rule.rule}}
}

// fireLocked 把事件置为 firing 并生成一条通知。
func (e *Engine) fireLocked(st *ruleState, now time.Time) []outgoing {
	ev := st.event
	ev.Status = statusFiring
	ev.Notified = true
	st.lastNotified = now
	return []outgoing{{event: *ev, rule: st.rule.rule}}
}

// silenced 当前是否处于 Ack 静默期。
func (e *Engine) silenced(st *ruleState, now time.Time) bool {
	return !st.silencedUntil.IsZero() && now.Before(st.silencedUntil)
}

// cooldownElapsed 距上次通知是否已过冷却期；规则没写 cooldown 时用全局默认值。
func (e *Engine) cooldownElapsed(st *ruleState, now time.Time) bool {
	cooldown := e.defaultCooldown
	if st.rule.cooldown > 0 {
		cooldown = st.rule.cooldown
	}
	if cooldown <= 0 {
		return true
	}
	return st.lastNotified.IsZero() || now.Sub(st.lastNotified) >= cooldown
}

// formatRuleDebug 规则的可读描述（日志排查用）。
func formatRuleDebug(cr *compiledRule) string {
	scope := formatScope(cr.rule.Scope)
	if scope == "" {
		scope = "全局"
	}
	return fmt.Sprintf("%s(metric=%s %s %s window=%s for=%s severity=%s scope=%s)",
		cr.rule.ID, cr.metric, cr.op, formatValue(cr.threshold),
		formatDuration(cr.window), formatDuration(cr.forDur), cr.severity, scope)
}
