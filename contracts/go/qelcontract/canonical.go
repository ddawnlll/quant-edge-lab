// Package qelcontract implements the QEL canonical identity contract:
// RFC 8785 JCS serialization, NFC validation, exact decimal strings and
// domain-limited integers.
package qelcontract

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

var (
	ErrCanonical   = errors.New("canonical error")
	decimalPattern = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$`)
	uintPattern    = regexp.MustCompile(`^(0|[1-9][0-9]*)$`)
	intPattern     = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)
)

const maxSafe = 9007199254740991

func checkText(s string) error {
	if !utf8Valid(s) {
		return fmt.Errorf("%w: invalid UTF-8", ErrCanonical)
	}
	if norm.NFC.String(s) != s {
		return fmt.Errorf("%w: non-NFC text", ErrCanonical)
	}
	return nil
}

func utf8Valid(s string) bool {
	return utf8.ValidString(s)
}

func quoteString(s string) (string, error) {
	if err := checkText(s); err != nil {
		return "", err
	}
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		default:
			if r < 0x20 {
				b.WriteString(fmt.Sprintf(`\u%04x`, r))
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String(), nil
}

func numberString(n json.Number) (string, error) {
	raw := string(n)
	if strings.ContainsAny(raw, ".eE") {
		f, err := strconv.ParseFloat(raw, 64)
		if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
			return "", fmt.Errorf("%w: nonfinite number", ErrCanonical)
		}
		if f == 0 {
			return "0", nil
		}
		if math.Abs(f) > maxSafe {
			return "", fmt.Errorf("%w: number outside JCS safe range", ErrCanonical)
		}
		return strconv.FormatFloat(f, 'g', -1, 64), nil
	}
	i, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || i > maxSafe || i < -maxSafe {
		return "", fmt.Errorf("%w: integer outside JCS safe range", ErrCanonical)
	}
	return strconv.FormatInt(i, 10), nil
}

func encode(v any, out *strings.Builder) error {
	switch x := v.(type) {
	case nil:
		out.WriteString("null")
	case bool:
		if x {
			out.WriteString("true")
		} else {
			out.WriteString("false")
		}
	case string:
		s, err := quoteString(x)
		if err != nil {
			return err
		}
		out.WriteString(s)
	case json.Number:
		s, err := numberString(x)
		if err != nil {
			return err
		}
		out.WriteString(s)
	case int:
		if x > maxSafe || x < -maxSafe {
			return fmt.Errorf("%w: integer outside JCS safe range", ErrCanonical)
		}
		out.WriteString(strconv.Itoa(x))
	case int64:
		if x > maxSafe || x < -maxSafe {
			return fmt.Errorf("%w: integer outside JCS safe range", ErrCanonical)
		}
		out.WriteString(strconv.FormatInt(x, 10))
	case []any:
		out.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				out.WriteByte(',')
			}
			if err := encode(item, out); err != nil {
				return err
			}
		}
		out.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			if err := checkText(k); err != nil {
				return err
			}
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool { return utf16Less(keys[i], keys[j]) })
		out.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				out.WriteByte(',')
			}
			s, err := quoteString(k)
			if err != nil {
				return err
			}
			out.WriteString(s)
			out.WriteByte(':')
			if err := encode(x[k], out); err != nil {
				return err
			}
		}
		out.WriteByte('}')
	default:
		return fmt.Errorf("%w: unsupported type %T", ErrCanonical, v)
	}
	return nil
}

func utf16Less(a, b string) bool {
	ua, ub := utf16.Encode([]rune(a)), utf16.Encode([]rune(b))
	for i := 0; i < len(ua) && i < len(ub); i++ {
		if ua[i] != ub[i] {
			return ua[i] < ub[i]
		}
	}
	return len(ua) < len(ub)
}

// CanonicalBytes serializes a decoded JSON value to RFC 8785 canonical UTF-8.
func CanonicalBytes(v any) ([]byte, error) {
	var b strings.Builder
	if err := encode(v, &b); err != nil {
		return nil, err
	}
	return []byte(b.String()), nil
}

// HashJCS returns lowercase SHA-256 hex of the canonical bytes.
func HashJCS(v any) (string, error) {
	b, err := CanonicalBytes(v)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

// DomainHash prepends a domain prefix to the canonical bytes.
func DomainHash(prefix string, v any) (string, error) {
	b, err := CanonicalBytes(v)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	h.Write([]byte(prefix))
	h.Write(b)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// ParseDecimal returns (coefficient, scale) for a canonical decimal string.
func ParseDecimal(s string) (int64, int, error) {
	if !decimalPattern.MatchString(s) {
		return 0, 0, fmt.Errorf("%w: non-canonical decimal %q", ErrCanonical, s)
	}
	if s == "-0" || strings.HasPrefix(s, "-0.") {
		frac := ""
		if i := strings.IndexByte(s, '.'); i >= 0 {
			frac = s[i+1:]
		}
		if strings.Trim(frac, "0") == "" {
			return 0, 0, fmt.Errorf("%w: negative zero", ErrCanonical)
		}
	}
	neg := strings.HasPrefix(s, "-")
	body := strings.TrimPrefix(s, "-")
	scale := 0
	if i := strings.IndexByte(body, '.'); i >= 0 {
		scale = len(body) - i - 1
		body = body[:i] + body[i+1:]
	}
	coeff, err := strconv.ParseInt(body, 10, 64)
	if err != nil {
		return 0, 0, fmt.Errorf("%w: decimal coefficient overflow", ErrCanonical)
	}
	if neg {
		coeff = -coeff
	}
	return coeff, scale, nil
}

// ParseUint validates a canonical unsigned integer string within a maximum.
func ParseUint(s string, maximum uint64) (uint64, error) {
	if !uintPattern.MatchString(s) {
		return 0, fmt.Errorf("%w: non-canonical unsigned integer %q", ErrCanonical, s)
	}
	v, err := strconv.ParseUint(s, 10, 64)
	if err != nil || v > maximum {
		return 0, fmt.Errorf("%w: unsigned integer overflow", ErrCanonical)
	}
	return v, nil
}

// ParseInt128Domain validates a canonical integer inside signed 128-bit bounds.
func ParseInt128Domain(s string) (string, error) {
	if !intPattern.MatchString(s) || s == "-0" {
		return "", fmt.Errorf("%w: non-canonical integer %q", ErrCanonical, s)
	}
	// Go has no int128; compare decimal length and lexicographic magnitude.
	neg := strings.HasPrefix(s, "-")
	body := strings.TrimPrefix(s, "-")
	limit := "170141183460469231731687303715884105727"
	limitNeg := "170141183460469231731687303715884105728"
	if len(body) > len(limit) || (len(body) == len(limit) && body > limit) ||
		(neg && (len(body) > len(limitNeg) || (len(body) == len(limitNeg) && body > limitNeg))) {
		return "", fmt.Errorf("%w: integer outside declared domain", ErrCanonical)
	}
	return s, nil
}
