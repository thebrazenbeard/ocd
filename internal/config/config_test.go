package config

import (
	"path/filepath"
	"testing"
)

func TestTypedRootsAndDefaults(t *testing.T) {
	root := t.TempDir()
	store, err := Open(filepath.Join(t.TempDir(), "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	added, err := store.AddRoot(Root{Path: root, MediaType: TV, Mode: Observe})
	if err != nil {
		t.Fatal(err)
	}
	if added.Provider != "tvmaze" {
		t.Fatalf("provider=%q", added.Provider)
	}
	if _, err := store.AddRoot(Root{Path: root, MediaType: Movie, Mode: Observe}); err == nil {
		t.Fatal("expected duplicate root rejection")
	}
}

func TestUpdateRootMode(t *testing.T) {
	root := t.TempDir()
	store, err := Open(filepath.Join(t.TempDir(), "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	added, err := store.AddRoot(Root{Path: root, MediaType: TV, Mode: Observe})
	if err != nil {
		t.Fatal(err)
	}
	updated, err := store.UpdateRootMode(added.ID, Apply)
	if err != nil {
		t.Fatal(err)
	}
	if updated.Mode != Apply || store.Snapshot().Roots[0].Mode != Apply {
		t.Fatalf("mode did not persist: %+v", updated)
	}
	if _, err := store.UpdateRootMode(added.ID, Mode("unsafe")); err == nil {
		t.Fatal("expected invalid mode rejection")
	}
}

func TestProviderCompatibility(t *testing.T) {
	root := Root{Path: filepath.Clean("/tmp/media"), MediaType: Music, Provider: "tmdb", Mode: Observe}
	if err := ValidateRoot(root); err == nil {
		t.Fatal("expected provider/media mismatch")
	}
}
