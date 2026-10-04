package config

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type MediaType string
type Mode string

const (
	TV    MediaType = "tv"
	Movie MediaType = "movie"
	Music MediaType = "music"

	Observe Mode = "observe"
	Apply   Mode = "apply"
)

type Root struct {
	ID        string    `json:"id"`
	Path      string    `json:"path"`
	MediaType MediaType `json:"media_type"`
	Provider  string    `json:"provider"`
	Mode      Mode      `json:"mode"`
	AddedAt   time.Time `json:"added_at"`
}

type Settings struct {
	Version         int    `json:"version"`
	TMDBBearer      string `json:"tmdb_bearer,omitempty"`
	SettleSeconds   int    `json:"settle_seconds"`
	ReconcileMinute int    `json:"reconcile_minutes"`
	Roots           []Root `json:"roots"`
}

type Store struct {
	mu   sync.RWMutex
	path string
	cfg  Settings
}

func Defaults() Settings {
	return Settings{Version: 1, SettleSeconds: 8, ReconcileMinute: 15, Roots: []Root{}}
}

func Open(path string) (*Store, error) {
	s := &Store{path: path, cfg: Defaults()}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		if err := s.persistLocked(); err != nil {
			return nil, err
		}
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, &s.cfg); err != nil {
		return nil, fmt.Errorf("decode config: %w", err)
	}
	if s.cfg.Version != 1 {
		return nil, fmt.Errorf("unsupported config version %d", s.cfg.Version)
	}
	if s.cfg.SettleSeconds < 2 {
		s.cfg.SettleSeconds = 8
	}
	if s.cfg.ReconcileMinute < 1 {
		s.cfg.ReconcileMinute = 15
	}
	for i := range s.cfg.Roots {
		if err := ValidateRoot(s.cfg.Roots[i]); err != nil {
			return nil, fmt.Errorf("root %q: %w", s.cfg.Roots[i].ID, err)
		}
	}
	return s, nil
}

func (s *Store) Snapshot() Settings {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := s.cfg
	out.Roots = append([]Root(nil), s.cfg.Roots...)
	return out
}

func (s *Store) AddRoot(root Root) (Root, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	canonical, err := filepath.Abs(filepath.Clean(root.Path))
	if err != nil {
		return Root{}, err
	}
	if resolved, resolveErr := filepath.EvalSymlinks(canonical); resolveErr == nil {
		canonical = resolved
	}
	root.Path = canonical
	if root.Provider == "" {
		root.Provider = DefaultProvider(root.MediaType)
	}
	if root.Mode == "" {
		root.Mode = Observe
	}
	if root.AddedAt.IsZero() {
		root.AddedAt = time.Now().UTC()
	}
	if root.ID == "" {
		sum := sha256.Sum256([]byte(strings.ToLower(root.Path) + "\x00" + string(root.MediaType)))
		root.ID = hex.EncodeToString(sum[:6])
	}
	if err := ValidateRoot(root); err != nil {
		return Root{}, err
	}
	for _, existing := range s.cfg.Roots {
		if existing.ID == root.ID || samePath(existing.Path, root.Path) {
			return Root{}, fmt.Errorf("root already registered: %s", existing.Path)
		}
		if pathContains(existing.Path, root.Path) || pathContains(root.Path, existing.Path) {
			return Root{}, fmt.Errorf("nested roots are not allowed: %s and %s", existing.Path, root.Path)
		}
	}
	s.cfg.Roots = append(s.cfg.Roots, root)
	if err := s.persistLocked(); err != nil {
		s.cfg.Roots = s.cfg.Roots[:len(s.cfg.Roots)-1]
		return Root{}, err
	}
	return root, nil
}

func (s *Store) UpdateRootMode(id string, mode Mode) (Root, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if mode != Observe && mode != Apply {
		return Root{}, fmt.Errorf("unsupported mode %q", mode)
	}
	for i := range s.cfg.Roots {
		if s.cfg.Roots[i].ID != id {
			continue
		}
		old := s.cfg.Roots[i]
		s.cfg.Roots[i].Mode = mode
		if err := s.persistLocked(); err != nil {
			s.cfg.Roots[i] = old
			return Root{}, err
		}
		return s.cfg.Roots[i], nil
	}
	return Root{}, os.ErrNotExist
}

func (s *Store) RemoveRoot(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := range s.cfg.Roots {
		if s.cfg.Roots[i].ID != id {
			continue
		}
		old := s.cfg.Roots[i]
		s.cfg.Roots = append(s.cfg.Roots[:i], s.cfg.Roots[i+1:]...)
		if err := s.persistLocked(); err != nil {
			s.cfg.Roots = append(s.cfg.Roots, Root{})
			copy(s.cfg.Roots[i+1:], s.cfg.Roots[i:])
			s.cfg.Roots[i] = old
			return err
		}
		return nil
	}
	return os.ErrNotExist
}

func (s *Store) SetTMDBBearer(token string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cfg.TMDBBearer = strings.TrimSpace(token)
	return s.persistLocked()
}

func (s *Store) RootForPath(path string) (Root, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, root := range s.cfg.Roots {
		if samePath(root.Path, path) || pathContains(root.Path, path) {
			return root, true
		}
	}
	return Root{}, false
}

func (s *Store) Path() string { return s.path }

func (s *Store) persistLocked() error {
	data, err := json.MarshalIndent(s.cfg, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func ValidateRoot(root Root) error {
	if root.Path == "" || !filepath.IsAbs(root.Path) {
		return errors.New("path must be absolute")
	}
	switch root.MediaType {
	case TV:
		if root.Provider != "" && root.Provider != "tvmaze" {
			return errors.New("tv roots currently require provider tvmaze")
		}
	case Movie:
		if root.Provider != "" && root.Provider != "tmdb" {
			return errors.New("movie roots currently require provider tmdb")
		}
	case Music:
		if root.Provider != "" && root.Provider != "embedded" {
			return errors.New("music roots currently require provider embedded")
		}
	default:
		return fmt.Errorf("unsupported media_type %q", root.MediaType)
	}
	if root.Mode != Observe && root.Mode != Apply {
		return fmt.Errorf("unsupported mode %q", root.Mode)
	}
	return nil
}

func DefaultProvider(mt MediaType) string {
	switch mt {
	case TV:
		return "tvmaze"
	case Movie:
		return "tmdb"
	case Music:
		return "embedded"
	default:
		return ""
	}
}

func samePath(a, b string) bool {
	aa, _ := filepath.Abs(filepath.Clean(a))
	bb, _ := filepath.Abs(filepath.Clean(b))
	return aa == bb
}

func pathContains(root, candidate string) bool {
	rootAbs, err := filepath.Abs(filepath.Clean(root))
	if err != nil {
		return false
	}
	candidateAbs, err := filepath.Abs(filepath.Clean(candidate))
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(rootAbs, candidateAbs)
	if err != nil {
		return false
	}
	return rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}
