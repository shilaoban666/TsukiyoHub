package alert

// 本文件负责「事件存储」：一个固定容量（默认 200）的环形缓冲。
//
// 为什么用环形而不是无限追加：告警引擎是常驻服务，事件只用于管理界面展示与排查，
// 无限增长会吃内存；需要长期留存请把同一份规则写到 Prometheus/Alertmanager。
//
// 事件状态机（与契约一致）：
//
//	   (条件满足)              for 到期               条件恢复
//	—> pending —————————————> firing —————————————> resolved
//	   (StartsAt)              (发通知)               (发「已恢复」)
//	      |                       |
//	      | Ack(确认)              | Ack(确认)
//	      v                       v
//	   acked <———————————————— acked
//	   (SilencedUntil 内不再通知，状态保留为 acked)
//
// pending 直接恢复（还没到 for）只会把事件标成 resolved，**不发通知**——
// 用户从没被告知过这条告警，再来一条「已恢复」只会制造噪音。

import (
	"strings"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// 事件状态常量。
const (
	statusPending  = "pending"
	statusFiring   = "firing"
	statusResolved = "resolved"
	statusAcked    = "acked"
)

// eventRing 事件环形缓冲（非并发安全：由 Engine 的互斥锁保护）。
type eventRing struct {
	buf  []*model.AlertEvent
	cap  int
	next int // 下一次写入位置
	size int // 当前条数
}

// newEventRing 创建环形缓冲；capacity <= 0 时用 200。
func newEventRing(capacity int) *eventRing {
	if capacity <= 0 {
		capacity = 200
	}
	return &eventRing{buf: make([]*model.AlertEvent, capacity), cap: capacity}
}

// add 追加事件；满了覆盖最旧的一条。保存指针，因为后续状态变化要就地更新。
func (r *eventRing) add(ev *model.AlertEvent) {
	if ev == nil || r.cap == 0 {
		return
	}
	r.buf[r.next] = ev
	r.next = (r.next + 1) % r.cap
	if r.size < r.cap {
		r.size++
	}
}

// all 按时间正序（最旧在前）返回全部事件指针。
func (r *eventRing) all() []*model.AlertEvent {
	out := make([]*model.AlertEvent, 0, r.size)
	if r.size == 0 {
		return out
	}
	start := (r.next - r.size + r.cap) % r.cap
	for i := 0; i < r.size; i++ {
		if ev := r.buf[(start+i)%r.cap]; ev != nil {
			out = append(out, ev)
		}
	}
	return out
}

// list 返回事件副本，最新在前；limit <= 0 = 50；status 为空 = 全部。
func (r *eventRing) list(limit int, status string) []model.AlertEvent {
	if limit <= 0 {
		limit = 50
	}
	status = strings.ToLower(strings.TrimSpace(status))
	out := make([]model.AlertEvent, 0, limit)
	all := r.all()
	for i := len(all) - 1; i >= 0 && len(out) < limit; i-- {
		ev := all[i]
		if status != "" && !strings.EqualFold(ev.Status, status) {
			continue
		}
		out = append(out, cloneEvent(ev))
	}
	return out
}

// get 按事件 id 取原始指针（Ack 需要就地修改状态）。
func (r *eventRing) get(id string) *model.AlertEvent {
	if strings.TrimSpace(id) == "" {
		return nil
	}
	for _, ev := range r.all() {
		if ev.ID == id {
			return ev
		}
	}
	return nil
}

// retain 只保留 keep 返回 true 的事件（规则被删除时清理它的事件）。
func (r *eventRing) retain(keep func(*model.AlertEvent) bool) {
	kept := make([]*model.AlertEvent, 0, r.cap)
	for _, ev := range r.all() {
		if keep(ev) {
			kept = append(kept, ev)
		}
	}
	r.buf = make([]*model.AlertEvent, r.cap)
	r.next = 0
	r.size = 0
	for _, ev := range kept {
		r.add(ev)
	}
}

// cloneEvent 复制事件（含 scope 与时间指针），避免外部改到引擎内部状态。
func cloneEvent(ev *model.AlertEvent) model.AlertEvent {
	out := *ev
	out.Scope = cloneScope(ev.Scope)
	if ev.EndsAt != nil {
		t := *ev.EndsAt
		out.EndsAt = &t
	}
	if ev.AckedAt != nil {
		t := *ev.AckedAt
		out.AckedAt = &t
	}
	if ev.SilencedUntil != nil {
		t := *ev.SilencedUntil
		out.SilencedUntil = &t
	}
	return out
}

// timePtr 取一个时间指针（避免多处重复写 &t 的临时变量）。
func timePtr(t time.Time) *time.Time { return &t }
