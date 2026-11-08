package control

import (
	"encoding/binary"
	"errors"
	"io"
)

var (
	ErrFrameTooLarge = errors.New("FRAME_TOO_LARGE")
	ErrFrameEmpty    = errors.New("FRAME_EMPTY")
)

// ReadFrame reads exactly one 4-byte big-endian length-prefixed frame.
// The length is validated before any payload allocation.
func ReadFrame(r io.Reader, max int) ([]byte, error) {
	var header [4]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(header[:])
	if n == 0 {
		return nil, ErrFrameEmpty
	}
	if int(n) > max {
		return nil, ErrFrameTooLarge
	}
	buf := make([]byte, int(n))
	if _, err := io.ReadFull(r, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

func WriteFrame(w io.Writer, payload []byte, max int) error {
	if len(payload) == 0 {
		return ErrFrameEmpty
	}
	if len(payload) > max {
		return ErrFrameTooLarge
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	if _, err := w.Write(header[:]); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}
