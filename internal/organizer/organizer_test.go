package organizer

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/media"
	"github.com/thebrazenbeard/ocd/internal/provider"
)

type fakeTV struct{}

func (fakeTV) Resolve(_ context.Context, hint media.TVHint) (provider.TVResult, error) {
	return provider.TVResult{ShowName: "Show", EpisodeName: "Episode Name", Score: 1, ProviderID: "1"}, nil
}

func TestApplyTVAndSidecar(t *testing.T) {
	root := t.TempDir()
	state := t.TempDir()
	cfg, err := config.Open(filepath.Join(state, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := cfg.AddRoot(config.Root{Path: root, MediaType: config.TV, Mode: config.Apply}); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(root, "Show.S01E02.mkv")
	sidecar := filepath.Join(root, "Show.S01E02.en.srt")
	if err := os.WriteFile(source, []byte("video"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(sidecar, []byte("subs"), 0o644); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-30 * time.Second)
	_ = os.Chtimes(source, old, old)
	_ = os.Chtimes(sidecar, old, old)

	org := New(cfg, state)
	org.TV = fakeTV{}
	plan := org.Process(context.Background(), source, "test")
	if plan.Status != "applied" {
		t.Fatalf("plan=%+v", plan)
	}
	target := filepath.Join(root, "Show", "Season 01", "S01E02 - Episode Name.mkv")
	if _, err := os.Stat(target); err != nil {
		t.Fatalf("target missing: %v", err)
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(target), "S01E02 - Episode Name.en.srt")); err != nil {
		t.Fatalf("sidecar missing: %v", err)
	}
	if _, err := os.Stat(source); !os.IsNotExist(err) {
		t.Fatalf("source still exists: %v", err)
	}
}

func TestMoveNoReplaceNeverClobbers(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source.mkv")
	target := filepath.Join(dir, "target.mkv")
	if err := os.WriteFile(source, []byte("source"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, []byte("target"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := moveNoReplace(source, target); err == nil {
		t.Fatal("expected no-replace collision")
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "target" {
		t.Fatalf("target was clobbered: %q", got)
	}
	if _, err := os.Stat(source); err != nil {
		t.Fatalf("source disappeared after rejected move: %v", err)
	}
}

func TestObserveDoesNotRename(t *testing.T) {
	root := t.TempDir()
	state := t.TempDir()
	cfg, _ := config.Open(filepath.Join(state, "config.json"))
	_, _ = cfg.AddRoot(config.Root{Path: root, MediaType: config.TV, Mode: config.Observe})
	source := filepath.Join(root, "Show.S01E01.mkv")
	_ = os.WriteFile(source, []byte("video"), 0o644)
	old := time.Now().Add(-30 * time.Second)
	_ = os.Chtimes(source, old, old)
	org := New(cfg, state)
	org.TV = fakeTV{}
	plan := org.Process(context.Background(), source, "test")
	if plan.Status != "observed" {
		t.Fatalf("plan=%+v", plan)
	}
	if _, err := os.Stat(source); err != nil {
		t.Fatalf("observe renamed source: %v", err)
	}
}
