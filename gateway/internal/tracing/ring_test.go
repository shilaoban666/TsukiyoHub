package tracing

import (
	"sync"
	"testing"
	"time"

	"tsukiyohub/gateway/internal/model"
)

// rec 构造一条测试记录。
func rec(id, route, modelID string, ok bool, at time.Time) model.RequestRecord {
	return model.RequestRecord{
		ID: id, Route: route, Model: modelID, OK: ok,
		Status: 200, At: at,
	}
}

func TestRingOverwritesOldest(t *testing.T) {
	ring := NewRing(3)
	base := time.Now()
	for i := 0; i < 5; i++ {
		ring.Add(rec("id"+string(rune('a'+i)), "fast", "m1", true, base.Add(time.Duration(i)*time.Second)))
	}
	if ring.Len() != 3 {
		t.Fatalf("容量 3 应只保留 3 条：%d", ring.Len())
	}
	list := ring.List(Filter{})
	if len(list) != 3 {
		t.Fatalf("List 条数不对：%d", len(list))
	}
	// 最新的在前：e, d, c
	if list[0].ID != "ide" || list[2].ID != "idc" {
		t.Fatalf("顺序不对：%v", []string{list[0].ID, list[1].ID, list[2].ID})
	}
	if _, ok := ring.Get("ida"); ok {
		t.Fatal("被覆盖的记录不应还能查到")
	}
	if got, ok := ring.Get("ide"); !ok || got.ID != "ide" {
		t.Fatalf("Get 最新记录失败：%+v %v", got, ok)
	}
	if _, ok := ring.Get("missing"); ok {
		t.Fatal("不存在的 ID 应返回 false")
	}
}

func TestRingFilters(t *testing.T) {
	ring := NewRing(10)
	now := time.Now()
	ring.Add(rec("1", "fast", "m1", true, now.Add(-10*time.Minute)))
	ring.Add(rec("2", "fast", "m2", false, now.Add(-2*time.Minute)))
	ring.Add(rec("3", "smart", "m2", true, now.Add(-1*time.Minute)))
	ring.Add(rec("4", "fast", "m2", true, now))

	onlyFail := false
	fails := ring.List(Filter{Status: &onlyFail})
	if len(fails) != 1 || fails[0].ID != "2" {
		t.Fatalf("只看失败不对：%+v", fails)
	}
	onlyOK := true
	oks := ring.List(Filter{Status: &onlyOK})
	if len(oks) != 3 {
		t.Fatalf("只看成功应有 3 条：%d", len(oks))
	}
	if got := ring.List(Filter{Route: "smart"}); len(got) != 1 || got[0].ID != "3" {
		t.Fatalf("按路由过滤不对：%+v", got)
	}
	if got := ring.List(Filter{Model: "m1"}); len(got) != 1 || got[0].ID != "1" {
		t.Fatalf("按模型过滤不对：%+v", got)
	}
	if got := ring.List(Filter{Since: now.Add(-3 * time.Minute)}); len(got) != 3 {
		t.Fatalf("按时间过滤不对：%d", len(got))
	}
	if got := ring.List(Filter{Limit: 2}); len(got) != 2 || got[0].ID != "4" {
		t.Fatalf("Limit 不对：%+v", got)
	}
	if got := ring.List(Filter{Route: "nope"}); len(got) != 0 {
		t.Fatalf("无匹配应为空：%+v", got)
	}
}

func TestRingZeroValueSafety(t *testing.T) {
	var ring *Ring
	ring.Add(rec("x", "r", "m", true, time.Now()))
	if ring.List(Filter{}) != nil || ring.Len() != 0 || ring.Capacity() != 0 {
		t.Fatal("nil Ring 应安全返回空")
	}
	if _, ok := ring.Get("x"); ok {
		t.Fatal("nil Ring 的 Get 应返回 false")
	}
	ring.Clear()
}

func TestRingClear(t *testing.T) {
	ring := NewRing(2)
	ring.Add(rec("a", "r", "m", true, time.Now()))
	ring.Clear()
	if ring.Len() != 0 || len(ring.List(Filter{})) != 0 {
		t.Fatal("Clear 后应为空")
	}
	if ring.Capacity() != 2 {
		t.Fatalf("容量不变：%d", ring.Capacity())
	}
}

func TestRingConcurrentAdd(t *testing.T) {
	ring := NewRing(100)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(worker int) {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				ring.Add(rec("w", "r", "m", j%2 == 0, time.Now()))
				_ = ring.List(Filter{Limit: 10})
			}
		}(i)
	}
	wg.Wait()
	if ring.Len() != 100 {
		t.Fatalf("并发写入后应满容量：%d", ring.Len())
	}
}

func TestNewRingDefaultCapacity(t *testing.T) {
	if cap := NewRing(0).Capacity(); cap != defaultCapacity {
		t.Fatalf("默认容量应为 %d：%d", defaultCapacity, cap)
	}
	if cap := NewRing(-5).Capacity(); cap != defaultCapacity {
		t.Fatalf("负数容量应回退默认值：%d", cap)
	}
}