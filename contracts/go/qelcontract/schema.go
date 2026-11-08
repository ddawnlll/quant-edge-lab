package qelcontract

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	jsonschema "github.com/santhosh-tekuri/jsonschema/v6"
	"golang.org/x/text/unicode/norm"
)

// Root locates the repository root containing specification/schemas.
func Root() (string, error) {
	if env := os.Getenv("QEL_ROOT"); env != "" {
		return env, nil
	}
	dir, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "specification", "schemas", "runtime.schema.json")); err == nil {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("cannot locate QEL root")
		}
		dir = parent
	}
}

func loadRuntimeSchema() (any, error) {
	root, err := Root()
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(filepath.Join(root, "specification", "schemas", "runtime.schema.json"))
	if err != nil {
		return nil, err
	}
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	return doc, nil
}

// ValidateDef validates one runtime $defs model and applies QEL semantic domains.
func ValidateDef(defName string, instance any) error {
	doc, err := loadRuntimeSchema()
	if err != nil {
		return err
	}
	compiler := jsonschema.NewCompiler()
	const resource = "urn:qel:runtime:1.1.0"
	if err := compiler.AddResource(resource, doc); err != nil {
		return err
	}
	schema, err := compiler.Compile(resource + "#/$defs/" + defName)
	if err != nil {
		return err
	}
	if err := schema.Validate(instance); err != nil {
		return err
	}
	rootMap, ok := doc.(map[string]any)
	if !ok {
		return fmt.Errorf("runtime schema is not an object")
	}
	return walkSemantics(instance, map[string]any{"$ref": "#/$defs/" + defName}, rootMap, "$")
}

func resolveSchema(schema map[string]any, root map[string]any) map[string]any {
	for i := 0; i < 32; i++ {
		ref, ok := schema["$ref"].(string)
		if !ok {
			return schema
		}
		if !strings.HasPrefix(ref, "#/") {
			return schema
		}
		var node any = root
		for _, part := range strings.Split(ref[2:], "/") {
			part = strings.ReplaceAll(strings.ReplaceAll(part, "~1", "/"), "~0", "~")
			m, ok := node.(map[string]any)
			if !ok {
				return schema
			}
			node = m[part]
		}
		next, ok := node.(map[string]any)
		if !ok {
			return schema
		}
		schema = next
	}
	return schema
}

func walkSemantics(instance any, schema map[string]any, root map[string]any, path string) error {
	schema = resolveSchema(schema, root)

	if subs, ok := schema["allOf"].([]any); ok {
		for _, sub := range subs {
			if m, ok := sub.(map[string]any); ok {
				if err := walkSemantics(instance, m, root, path); err != nil {
					return err
				}
			}
		}
	}
	switch instance := instance.(type) {
	case map[string]any:
		props, _ := schema["properties"].(map[string]any)
		addl, _ := schema["additionalProperties"].(map[string]any)
		for k, v := range instance {
			if norm.NFC.String(k) != k {
				return fmt.Errorf("%s: non-NFC key %q", path, k)
			}
			if sub, ok := props[k].(map[string]any); ok {
				if err := walkSemantics(v, sub, root, path+"."+k); err != nil {
					return err
				}
			} else if addl != nil {
				if err := walkSemantics(v, addl, root, path+"."+k); err != nil {
					return err
				}
			}
		}
	case []any:
		if item, ok := schema["items"].(map[string]any); ok {
			for i, v := range instance {
				if err := walkSemantics(v, item, root, fmt.Sprintf("%s[%d]", path, i)); err != nil {
					return err
				}
			}
		}
	case string:
		if norm.NFC.String(instance) != instance {
			return fmt.Errorf("%s: non-NFC text", path)
		}
		if dom, ok := schema["x-qel-integer"].(map[string]any); ok {
			if !intPattern.MatchString(instance) || instance == "-0" {
				return fmt.Errorf("%s: non-canonical integer %q", path, instance)
			}
			if _, err := ParseInt128Domain(instance); err != nil {
				return err
			}
			minS, _ := dom["minimum"].(string)
			maxS, _ := dom["maximum"].(string)
			if minS != "" && minS[0] != '-' && strings.HasPrefix(instance, "-") {
				return fmt.Errorf("%s: integer below declared domain", path)
			}
			if minS != "" && compareSignedDecimal(instance, minS) < 0 {
				return fmt.Errorf("%s: integer below declared domain", path)
			}
			if maxS != "" && compareSignedDecimal(instance, maxS) > 0 {
				return fmt.Errorf("%s: integer above declared domain", path)
			}
		}
		if _, ok := schema["x-qel-decimal"].(map[string]any); ok {
			if _, _, err := ParseDecimal(instance); err != nil {
				return err
			}
		}
	}
	return nil
}

func compareSignedDecimal(a, b string) int {
	an, aok := newBigInt(a)
	bn, bok := newBigInt(b)
	if !aok || !bok {
		return strings.Compare(a, b)
	}
	return an.Cmp(bn)
}
