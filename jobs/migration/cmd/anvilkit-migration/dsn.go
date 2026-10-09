package main

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
)

// migrationDSN resolves the migrator DSN from exactly one source (the value
// or the mounted secret file, read once and trimmed) and applies the
// data-plane TLS rule (P0.6): outside development it must name
// sslmode=verify-full. No error echoes the DSN, which carries the password.
// Control's migration Job applies the same rule (its internal/config).
func migrationDSN(dsn, dsnFile string, development bool) (string, error) {
	switch {
	case dsn != "" && dsnFile != "":
		return "", errors.New("-dsn (ANVILKIT_MIGRATION_DSN) and -dsn-file (ANVILKIT_MIGRATION_DSN_FILE) are mutually exclusive")
	case dsnFile != "":
		raw, err := os.ReadFile(dsnFile)
		if err != nil {
			return "", fmt.Errorf("-dsn-file: %w", err)
		}
		if dsn = strings.TrimSpace(string(raw)); dsn == "" {
			return "", fmt.Errorf("-dsn-file: %s is empty", dsnFile)
		}
	case dsn == "":
		return "", errors.New("-dsn (ANVILKIT_MIGRATION_DSN) or -dsn-file (ANVILKIT_MIGRATION_DSN_FILE) is required")
	}
	if development {
		return dsn, nil
	}
	modes, err := sslModes(dsn)
	if err != nil {
		return "", errors.New("dsn: not a parseable PostgreSQL DSN (URL or keyword/value form)")
	}
	if len(modes) == 0 {
		modes = []string{""}
	}
	for _, mode := range modes {
		if mode != "verify-full" {
			return "", fmt.Errorf("dsn: sslmode must be verify-full outside development (got %q); the development foundation's DSN needs -development or ANVILKIT_MIGRATION_DEVELOPMENT=true (DEVELOPMENT_ONLY)", mode)
		}
	}
	return dsn, nil
}

var errDSN = errors.New("unparseable DSN")

// sslModes returns every sslmode the DSN itself names, in order, following
// pgx's tokenization (pgconn.ParseConfig) of the URL form
// (postgres://…?sslmode=…) and the keyword/value form (… sslmode=…). Every
// occurrence counts (a repeated key never hides a weaker mode) and the URL
// alias ssl=true reads as require; PGSSLMODE and service files never count.
func sslModes(dsn string) ([]string, error) {
	if strings.IndexByte(dsn, 0) >= 0 {
		return nil, errDSN
	}
	rest, ok := strings.CutPrefix(dsn, "postgresql://")
	if !ok {
		rest, ok = strings.CutPrefix(dsn, "postgres://")
	}
	if !ok {
		return keywordSSLModes(dsn)
	}
	// Like libpq, a '@' before the first '/' ends the userinfo; the query
	// follows the first '?' after it.
	if i := strings.IndexAny(rest, "@/"); i >= 0 && rest[i] == '@' {
		rest = rest[i+1:]
	}
	_, query, ok := strings.Cut(rest, "?")
	if !ok {
		return nil, nil
	}
	var modes []string
	for query != "" {
		var pair string
		pair, query, _ = strings.Cut(query, "&")
		rawKey, rawValue, found := strings.Cut(pair, "=")
		if !found || strings.Contains(rawValue, "=") {
			return nil, errDSN
		}
		key, err := url.PathUnescape(strings.Trim(rawKey, " "))
		if err != nil {
			return nil, errDSN
		}
		value, err := url.PathUnescape(strings.Trim(rawValue, " "))
		if err != nil {
			return nil, errDSN
		}
		switch {
		case key == "sslmode":
			modes = append(modes, value)
		case key == "ssl" && value == "true":
			modes = append(modes, "require")
		}
	}
	return modes, nil
}

const dsnSpace = " \t\n\r\v\f"

// keywordSSLModes reads a keyword/value DSN: key = value pairs separated by
// whitespace, a value either single-quoted or bare, '\' escaping the next
// byte in both.
func keywordSSLModes(s string) ([]string, error) {
	var modes []string
	s = strings.TrimLeft(s, dsnSpace)
	for s != "" {
		eq := strings.IndexByte(s, '=')
		if eq < 0 {
			return nil, errDSN
		}
		key := strings.Trim(s[:eq], dsnSpace)
		if key == "" || strings.ContainsAny(key, dsnSpace) {
			return nil, errDSN
		}
		s = strings.TrimLeft(s[eq+1:], dsnSpace)
		var value strings.Builder
		if strings.HasPrefix(s, "'") {
			end := 1
			for ; end < len(s) && s[end] != '\''; end++ {
				if s[end] == '\\' {
					if end++; end == len(s) {
						break
					}
				}
				value.WriteByte(s[end])
			}
			if end >= len(s) {
				return nil, errDSN
			}
			s = s[end+1:]
		} else {
			end := 0
			for ; end < len(s) && !strings.ContainsRune(dsnSpace, rune(s[end])); end++ {
				if s[end] == '\\' {
					if end++; end == len(s) {
						break
					}
				}
				value.WriteByte(s[end])
			}
			s = s[min(end, len(s)):]
		}
		s = strings.TrimLeft(s, dsnSpace)
		if key == "sslmode" {
			modes = append(modes, value.String())
		}
	}
	return modes, nil
}
