package bootstrap

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/thebrazenbeard/ocd/internal/config"
)

func TestApplyBootstrapWaitsForShareThenInitializes(t *testing.T) {
	stateDir := t.TempDir()
	sharesDir := t.TempDir()
	spec := Spec{Share: "TV Shows", MediaType: config.TV, Mode: config.Observe}
	if err := Stage(stateDir, spec); err != nil {
		t.Fatal(err)
	}

	applied, err := Apply(stateDir, sharesDir)
	if err == nil || applied {
		t.Fatalf("expected missing share to defer bootstrap, applied=%v err=%v", applied, err)
	}
	if _, err := os.Stat(filepath.Join(stateDir, bootstrapFile)); err != nil {
		t.Fatalf("bootstrap should remain for retry: %v", err)
	}

	sharePath := filepath.Join(sharesDir, spec.Share)
	if err := os.MkdirAll(sharePath, 0o755); err != nil {
		t.Fatal(err)
	}
	applied, err = Apply(stateDir, sharesDir)
	if err != nil || !applied {
		t.Fatalf("apply bootstrap: applied=%v err=%v", applied, err)
	}
	if _, err := os.Stat(filepath.Join(stateDir, bootstrapFile)); !os.IsNotExist(err) {
		t.Fatalf("bootstrap should be removed after success: %v", err)
	}

	store, err := config.Open(filepath.Join(stateDir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	got := store.Snapshot()
	if len(got.Roots) != 1 {
		t.Fatalf("roots=%d", len(got.Roots))
	}
	wantPath, _ := filepath.Abs(sharePath)
	if got.Roots[0].Path != wantPath || got.Roots[0].MediaType != config.TV ||
		got.Roots[0].Mode != config.Observe || got.Roots[0].Provider != "tvmaze" {
		t.Fatalf("root=%+v", got.Roots[0])
	}
}

func TestApplyBootstrapIsRetrySafeAfterPartialSuccess(t *testing.T) {
	stateDir := t.TempDir()
	sharesDir := t.TempDir()
	sharePath := filepath.Join(sharesDir, "Movies")
	if err := os.MkdirAll(sharePath, 0o755); err != nil {
		t.Fatal(err)
	}
	spec := Spec{
		Share: "Movies", MediaType: config.Movie, Mode: config.Observe,
		TMDBBearer: "secret-token",
	}
	if err := Stage(stateDir, spec); err != nil {
		t.Fatal(err)
	}
	if applied, err := Apply(stateDir, sharesDir); err != nil || !applied {
		t.Fatalf("first apply: applied=%v err=%v", applied, err)
	}

	if err := Stage(stateDir, spec); err != nil {
		t.Fatal(err)
	}
	if applied, err := Apply(stateDir, sharesDir); err != nil || !applied {
		t.Fatalf("retry apply: applied=%v err=%v", applied, err)
	}
	store, err := config.Open(filepath.Join(stateDir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	got := store.Snapshot()
	if len(got.Roots) != 1 {
		t.Fatalf("retry duplicated root: %+v", got.Roots)
	}
	if got.TMDBBearer != "secret-token" {
		t.Fatalf("tmdb token not persisted")
	}
}

func TestStageRejectsShareTraversal(t *testing.T) {
	for _, share := range []string{"", ".", "..", "../TV", "TV/Shows", "TV\\Shows"} {
		err := Stage(t.TempDir(), Spec{Share: share, MediaType: config.TV, Mode: config.Observe})
		if err == nil {
			t.Fatalf("share %q should be rejected", share)
		}
	}
}
