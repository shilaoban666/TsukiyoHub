// Package tracing 提供请求追踪的环形缓冲（最近 N 条 RequestRecord）。
//
// 为什么用环形缓冲而不是数据库：网关要求零依赖、单进程可跑；排障只需要
// 「最近几千条」，定长环形数组的写入是 O(1) 且内存上界固定，不会被突发流量拖垮。
// 需要长期留存时由调用方把记录另外写入日志/外部系统。
package tracing

import (
	"sync"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// defaultCapacity 未指定容量时的默认值。
const defaultCapacity = 2000

// Filter 追踪查询过滤条件（零值 = 不过滤）。
type Filter struct {
	// Limit 最多返回多少条（<=0 = 不限）
	Limit int
	// Status true=只看成功，false=只看失败，nil=全部
	Status *bool
	// Route 路由别名精确匹配
	Route string
	// Model 实际命中的模型精确匹配
	Model string
	// Since 只看该时间之后（含）的记录；零值 = 不限
	Since time.Time
}

// Ring 请求追踪环形缓冲（并发安全）。
type Ring struct {
	mu       sync.RWMutex
	buf      []model.RequestRecord
	next     int // 下一条写入位置
	size     int
	capacity int
}

// NewRing 创建环形缓冲；capacity <= 0 时使用默认容量 2000。
func NewRing(capacity int) *Ring {
	if capacity <= 0 {
		capacity = defaultCapacity
	}
	return &Ring{buf: make([]model.RequestRecord, capacity), capacity: capacity}
}

// Capacity 返回容量。
func (r *Ring) Capacity() int {
	if r == nil {
		return 0
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.capacity
}

// Len 返回当前条数。
func (r *Ring) Len() int {
	if r == nil {
		return 0
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.size
}

// Add 写入一条记录；满了覆盖最旧的一条。
func (r *Ring) Add(rec model.RequestRecord) {
	if r == nil {
		return
	}
	if rec.At.IsZero() {
		rec.At = time.Now()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.buf[r.next] = rec
	r.next = (r.next + 1) % r.capacity
	if r.size < r.capacity {
		r.size++
	}
}

// List 按过滤条件返回记录，**最新的在前**。
func (r *Ring) List(filter Filter) []model.RequestRecord {
	if r == nil {
		return nil
	}
	limit := filter.Limit
	if limit <= 0 {
		limit = r.capacity
	}
	out := make([]model.RequestRecord, 0, minInt(limit, r.capacity))

	r.mu.RLock()
	defer r.mu.RUnlock()
	for i := 0; i < r.size && len(out) < limit; i++ {
		// 从最新往回走：next-1 是最新一条。
		idx := (r.next - 1 - i + r.capacity*2) % r.capacity
		rec := r.buf[idx]
		if !matchFilter(rec, filter) {
			continue
		}
		out = append(out, rec)
	}
	return out
}

// Get 按请求 ID 查单条记录。
func (r *Ring) Get(id string) (model.RequestRecord, bool) {
	if r == nil || id == "" {
		return model.RequestRecord{}, false
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	for i := 0; i < r.size; i++ {
		idx := (r.next - 1 - i + r.capacity*2) % r.capacity
		if r.buf[idx].ID == id {
			return r.buf[idx], true
		}
	}
	return model.RequestRecord{}, false
}

// Clear 清空全部记录（测试/管理用）。
func (r *Ring) Clear() {
	if r == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.next = 0
	r.size = 0
}

// matchFilter 判断记录是否满足过滤条件。
func matchFilter(rec model.RequestRecord, filter Filter) bool {
	if filter.Status != nil && rec.OK != *filter.Status {
		return false
	}
	if filter.Route != "" && rec.Route != filter.Route {
		return false
	}
	if filter.Model != "" && rec.Model != filter.Model {
		return false
	}
	if !filter.Since.IsZero() && rec.At.Before(filter.Since) {
		return false
	}
	return true
}

// minInt 取较小值。
func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}