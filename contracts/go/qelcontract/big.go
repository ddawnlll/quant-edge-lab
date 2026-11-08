package qelcontract

import "math/big"

func newBigInt(s string) (*big.Int, bool) {
	n := new(big.Int)
	_, ok := n.SetString(s, 10)
	return n, ok
}
