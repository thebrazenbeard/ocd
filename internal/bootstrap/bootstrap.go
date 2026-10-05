package bootstrap

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/organizer"
)

const bootstrapFile = "bootstrap.json"

type Spec struct {
	Share      string           `json:"share"`
	MediaType  config.MediaType `json:"media_type"`
	Mode       config.Mode      `json:"mode"`
	TMDBBearer string           `json:"tmdb_bearer,omitempty"`
}

func Stage(stateDir string, spec Spec) error {
	if err := validateSpec(&spec); err != nil {
		return err
	}
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(spec, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	path := filepath.Join(stateDir, bootstrapFile)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

func Apply(stateDir, sharesDir string) (bool, error) {
	path := filepath.Join(stateDir, bootstrapFile)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var spec Spec
	if err := json.Unmarshal(data, &spec); err != nil {
		return false, fmt.Errorf("decode bootstrap: %w", err)
	}
	if err := validateSpec(&spec); err != nil {
		return false, err
	}

	sharePath := filepath.Join(sharesDir, spec.Share)
	info, err := os.Stat(sharePath)
	if err != nil {
		return false, fmt.Errorf("DSM data-share is not ready: %w", err)
	}
	if !info.IsDir() {
		return false, fmt.Errorf("DSM data-share is not a directory: %s", sharePath)
	}

	cfg, err := config.Open(filepath.Join(stateDir, "config.json"))
	if err != nil {
		return false, err
	}
	root := config.Root{
		Path: sharePath, MediaType: spec.MediaType, Mode: spec.Mode,
		Provider: config.DefaultProvider(spec.MediaType),
	}
	org := organizer.New(cfg, stateDir)
	if err := org.ValidateRootAccess(root); err != nil {
		return false, fmt.Errorf("validate staged root: %w", err)
	}

	canonical, err := canonicalPath(sharePath)
	if err != nil {
		return false, err
	}
	found := false
	for _, existing := range cfg.Snapshot().Roots {
		existingPath, existingErr := canonicalPath(existing.Path)
		if existingErr != nil || existingPath != canonical {
			continue
		}
		found = true
		if existing.MediaType != root.MediaType || existing.Mode != root.Mode || existing.Provider != root.Provider {
			return false, fmt.Errorf("staged root conflicts with existing root %s", existing.Path)
		}
		break
	}
	if !found {
		if _, err := cfg.AddRoot(root); err != nil {
			return false, err
		}
	}
	if spec.TMDBBearer != "" {
		if err := cfg.SetTMDBBearer(spec.TMDBBearer); err != nil {
			return false, err
		}
	}
	if err := os.Remove(path); err != nil {
		return false, err
	}
	return true, nil
}

func validateSpec(spec *Spec) error {
	if strings.TrimSpace(spec.Share) == "" || spec.Share == "." || spec.Share == ".." ||
		strings.ContainsAny(spec.Share, "/\\") {
		return fmt.Errorf("invalid DSM shared-folder name %q", spec.Share)
	}
	if config.DefaultProvider(spec.MediaType) == "" {
		return fmt.Errorf("unsupported media type %q", spec.MediaType)
	}
	if spec.Mode == "" {
		spec.Mode = config.Observe
	}
	if spec.Mode != config.Observe && spec.Mode != config.Apply {
		return fmt.Errorf("unsupported mode %q", spec.Mode)
	}
	return nil
}

func canonicalPath(path string) (string, error) {
	absolute, err := filepath.Abs(filepath.Clean(path))
	if err != nil {
		return "", err
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", err
	}
	return filepath.Clean(resolved), nil
}
