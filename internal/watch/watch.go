package watch

import (
	"context"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"

	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/media"
	"github.com/thebrazenbeard/ocd/internal/organizer"
)

type Manager struct {
	Config    *config.Store
	Organizer *organizer.Organizer

	mu      sync.Mutex
	watcher *fsnotify.Watcher
	watched map[string]struct{}
	pending map[string]*time.Timer
}

func New(cfg *config.Store, org *organizer.Organizer) (*Manager, error) {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	return &Manager{
		Config: cfg, Organizer: org, watcher: w,
		watched: map[string]struct{}{}, pending: map[string]*time.Timer{},
	}, nil
}

func (m *Manager) Close() error { return m.watcher.Close() }

func (m *Manager) Run(ctx context.Context) {
	if err := m.Refresh(); err != nil {
		log.Printf("initial watcher refresh: %v", err)
	}
	reconcileTicker := time.NewTicker(time.Duration(m.Config.Snapshot().ReconcileMinute) * time.Minute)
	refreshTicker := time.NewTicker(30 * time.Second)
	defer reconcileTicker.Stop()
	defer refreshTicker.Stop()

	go m.Reconcile(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case err := <-m.watcher.Errors:
			if err != nil {
				log.Printf("watcher: %v", err)
			}
		case event, ok := <-m.watcher.Events:
			if !ok {
				return
			}
			m.handle(ctx, event)
		case <-refreshTicker.C:
			if err := m.Refresh(); err != nil {
				log.Printf("watcher refresh: %v", err)
			}
		case <-reconcileTicker.C:
			go m.Reconcile(ctx)
		}
	}
}

func (m *Manager) Refresh() error {
	cfg := m.Config.Snapshot()
	wanted := map[string]struct{}{}
	for _, root := range cfg.Roots {
		err := filepath.WalkDir(root.Path, func(path string, entry fs.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if entry.IsDir() {
				wanted[path] = struct{}{}
			}
			return nil
		})
		if err != nil {
			log.Printf("walk root %s: %v", root.Path, err)
		}
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	for path := range wanted {
		if _, ok := m.watched[path]; ok {
			continue
		}
		if err := m.watcher.Add(path); err != nil {
			log.Printf("add watch %s: %v", path, err)
			continue
		}
		m.watched[path] = struct{}{}
	}
	for path := range m.watched {
		if _, ok := wanted[path]; ok {
			continue
		}
		_ = m.watcher.Remove(path)
		delete(m.watched, path)
	}
	return nil
}

func (m *Manager) Reconcile(ctx context.Context) {
	cfg := m.Config.Snapshot()
	for _, root := range cfg.Roots {
		_ = filepath.WalkDir(root.Path, func(path string, entry fs.DirEntry, err error) error {
			if err != nil || entry.IsDir() || !media.Supported(path) {
				return nil
			}
			select {
			case <-ctx.Done():
				return context.Canceled
			default:
			}
			info, statErr := entry.Info()
			if statErr != nil {
				return nil
			}
			if time.Since(info.ModTime()) < time.Duration(cfg.SettleSeconds)*time.Second {
				return nil
			}
			m.Organizer.Process(ctx, path, "reconcile")
			return nil
		})
	}
}

func (m *Manager) handle(ctx context.Context, event fsnotify.Event) {
	if event.Has(fsnotify.Create) {
		if info, err := os.Stat(event.Name); err == nil && info.IsDir() {
			_ = filepath.WalkDir(event.Name, func(path string, entry fs.DirEntry, err error) error {
				if err == nil && entry.IsDir() {
					m.mu.Lock()
					if _, ok := m.watched[path]; !ok {
						if addErr := m.watcher.Add(path); addErr == nil {
							m.watched[path] = struct{}{}
						}
					}
					m.mu.Unlock()
				}
				return nil
			})
			return
		}
	}
	if !(event.Has(fsnotify.Create) || event.Has(fsnotify.Write) || event.Has(fsnotify.Rename)) {
		return
	}
	if !media.Supported(event.Name) {
		return
	}
	m.schedule(ctx, event.Name)
}

func (m *Manager) schedule(ctx context.Context, path string) {
	delay := time.Duration(m.Config.Snapshot().SettleSeconds) * time.Second
	m.mu.Lock()
	if old := m.pending[path]; old != nil {
		old.Stop()
	}
	m.pending[path] = time.AfterFunc(delay, func() {
		m.mu.Lock()
		delete(m.pending, path)
		m.mu.Unlock()
		info1, err := os.Stat(path)
		if err != nil || !info1.Mode().IsRegular() {
			return
		}
		time.Sleep(750 * time.Millisecond)
		info2, err := os.Stat(path)
		if err != nil || info1.Size() != info2.Size() || !info1.ModTime().Equal(info2.ModTime()) {
			m.schedule(ctx, path)
			return
		}
		m.Organizer.Process(ctx, path, "watch")
	})
	m.mu.Unlock()
}
