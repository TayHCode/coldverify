package calc

import "testing"

func TestZero(t *testing.T) {
	if Add(0, 0) != 0 {
		t.Fatal("zero")
	}
}

func TestAdd(t *testing.T) {
	if got := Add(2, 3); got != 5 {
		t.Fatalf("Add(2, 3) = %d", got)
	}
}
