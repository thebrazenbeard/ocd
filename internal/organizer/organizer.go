package organizer

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/media"
	"github.com/thebrazenbeard/ocd/internal/provider"
)

type Plan struct {
	ID         string           `json:"id"`
	At         time.Time        `json:"at"`
	RootID     string           `json:"root_id"`
	MediaType  config.MediaType `json:"media_type"`
	Mode       config.Mode      `json:"mode"`
	Source     string           `json:"source"`
	Target     string           `json:"target,omitempty"`
	Status     string           `json:"status"`
	Reason     string           `json:"reason,omitempty"`
	Provider   string           `json:"provider,omitempty"`
	ProviderID string           `json:"provider_id,omitempty"`
	Score      float64          `json:"score,omitempty"`
	Operations []Operation      `json:"operations,omitempty"`
}

type Operation struct {
	From string `json:"from"`
	To   string `json:"to"`
}

type Finding struct {
	At     time.Time `json:"at"`
	Path   string    `json:"path"`
	Kind   string    `json:"kind"`
	Detail string    `json:"detail"`
}

type Organizer struct {
	Config   *config.Store
	StateDir string
	TV       provider.TV

	mu       sync.RWMutex
	plans    []Plan
	findings []Finding
	recent   map[string]time.Time
	inFlight map[string]struct{}
}

func New(cfg *config.Store, stateDir string) *Organizer {
	return &Organizer{
		Config:   cfg,
		StateDir: stateDir,
		TV:       provider.TVMaze{},
		recent:   make(map[string]time.Time),
		inFlight: make(map[string]struct{}),
	}
}

func (o *Organizer) Process(ctx context.Context, path, source string) Plan {
	root, ok := o.Config.RootForPath(path)
	if !ok {
		return Plan{At: time.Now().UTC(), Source: path, Status: "ignored", Reason: "path is not under a registered root"}
	}
	plan := Plan{
		At: time.Now().UTC(), RootID: root.ID, MediaType: root.MediaType,
		Mode: root.Mode, Source: path, Provider: root.Provider,
	}
	plan.ID = planID(plan.At, path)

	if !o.beginProcess(path) {
		plan.Status = "ignored"
		plan.Reason = "path is already being processed"
		return plan
	}
	defer o.endProcess(path)

	if o.suppressed(path) {
		plan.Status = "ignored"
		plan.Reason = "recent OCD-originated path"
		return plan
	}
	info, err := os.Lstat(path)
	if err != nil {
		plan.Status = "ignored"
		plan.Reason = err.Error()
		return o.record(plan)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		plan.Status = "held"
		plan.Reason = "symbolic-link media inputs are not followed"
		o.addFinding(path, "symlink", plan.Reason)
		return o.record(plan)
	}
	if !info.Mode().IsRegular() || !media.Supported(path) {
		plan.Status = "ignored"
		plan.Reason = "not a supported regular media file"
		return o.record(plan)
	}
	settle := time.Duration(o.Config.Snapshot().SettleSeconds) * time.Second
	if time.Since(info.ModTime()) < settle {
		plan.Status = "held"
		plan.Reason = "file has not passed the stability interval"
		return o.record(plan)
	}

	switch root.MediaType {
	case config.TV:
		plan = o.planTV(ctx, root, path, plan)
	case config.Movie:
		plan = o.planMovie(ctx, root, path, plan)
	case config.Music:
		plan = o.planMusic(root, path, plan)
	default:
		plan.Status = "held"
		plan.Reason = "unsupported media type"
	}
	if plan.Status != "planned" || root.Mode != config.Apply {
		if plan.Status == "planned" && root.Mode == config.Observe {
			plan.Status = "observed"
		}
		return o.record(plan)
	}
	if err := o.apply(&plan); err != nil {
		plan.Status = "error"
		plan.Reason = err.Error()
		o.addFinding(path, "apply-error", err.Error())
	} else {
		plan.Status = "applied"
	}
	return o.record(plan)
}

func (o *Organizer) planTV(ctx context.Context, root config.Root, path string, plan Plan) Plan {
	if media.LooksCanonicalTV(root.Path, path) {
		plan.Status = "noop"
		plan.Reason = "already organized"
		return plan
	}
	if !media.Video(path) {
		plan.Status = "ignored"
		plan.Reason = "tv root received non-video file"
		return plan
	}
	hint, err := media.ParseTV(path)
	if err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		o.addFinding(path, "parse", err.Error())
		return plan
	}
	result, err := o.TV.Resolve(ctx, hint)
	if err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		o.addFinding(path, "metadata", err.Error())
		return plan
	}
	plan.Target = media.TVTarget(
		root.Path, result.ShowName, hint.Season, hint.Episode,
		result.EpisodeName, strings.ToLower(filepath.Ext(path)),
	)
	plan.ProviderID = result.ProviderID
	plan.Score = result.Score
	return o.finishPlan(root, path, plan)
}

func (o *Organizer) planMovie(ctx context.Context, root config.Root, path string, plan Plan) Plan {
	if media.LooksCanonicalMovie(root.Path, path) {
		plan.Status = "noop"
		plan.Reason = "already organized"
		return plan
	}
	if !media.Video(path) {
		plan.Status = "ignored"
		plan.Reason = "movie root received non-video file"
		return plan
	}
	hint, err := media.ParseMovie(path)
	if err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		o.addFinding(path, "parse", err.Error())
		return plan
	}
	resolver := provider.TMDB{Bearer: o.Config.Snapshot().TMDBBearer}
	result, err := resolver.Resolve(ctx, hint)
	if err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		o.addFinding(path, "metadata", err.Error())
		return plan
	}
	plan.Target = media.MovieTarget(root.Path, result.Title, result.Year, strings.ToLower(filepath.Ext(path)))
	plan.ProviderID = result.ProviderID
	plan.Score = result.Score
	return o.finishPlan(root, path, plan)
}

func (o *Organizer) planMusic(root config.Root, path string, plan Plan) Plan {
	if !media.Audio(path) {
		plan.Status = "ignored"
		plan.Reason = "music root received non-audio file"
		return plan
	}
	result, err := provider.ReadMusic(path)
	if err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		o.addFinding(path, "metadata", err.Error())
		return plan
	}
	plan.Target = media.MusicTarget(
		root.Path, result.Artist, result.Album, result.Title,
		strings.ToLower(filepath.Ext(path)),
	)
	return o.finishPlan(root, path, plan)
}

func (o *Organizer) finishPlan(root config.Root, source string, plan Plan) Plan {
	sourceAbs, _ := filepath.Abs(filepath.Clean(source))
	targetAbs, _ := filepath.Abs(filepath.Clean(plan.Target))
	if samePath(sourceAbs, targetAbs) {
		plan.Status = "noop"
		plan.Reason = "already organized"
		plan.Target = targetAbs
		return plan
	}
	if !inside(root.Path, targetAbs) {
		plan.Status = "held"
		plan.Reason = "target escapes registered root"
		return plan
	}
	if err := validateExistingTargetParent(root.Path, targetAbs); err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		o.addFinding(source, "target-path", err.Error())
		return plan
	}
	if _, err := os.Stat(targetAbs); err == nil {
		plan.Status = "held"
		plan.Reason = "target already exists"
		o.addFinding(source, "collision", plan.Reason)
		return plan
	} else if !errors.Is(err, os.ErrNotExist) {
		plan.Status = "held"
		plan.Reason = err.Error()
		return plan
	}
	ops, err := companionOperations(sourceAbs, targetAbs)
	if err != nil {
		plan.Status = "held"
		plan.Reason = err.Error()
		return plan
	}
	plan.Source = sourceAbs
	plan.Target = targetAbs
	plan.Operations = ops
	plan.Status = "planned"
	return plan
}

func (o *Organizer) apply(plan *Plan) error {
	if len(plan.Operations) == 0 {
		return errors.New("no operations")
	}
	for _, op := range plan.Operations {
		if _, err := os.Stat(op.To); err == nil {
			return fmt.Errorf("refusing overwrite: %s", op.To)
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	if err := o.appendJournal("intent", *plan); err != nil {
		return err
	}
	completed := make([]Operation, 0, len(plan.Operations))
	for _, op := range plan.Operations {
		if err := os.MkdirAll(filepath.Dir(op.To), 0o755); err != nil {
			rollback(completed)
			return err
		}
		if err := moveNoReplace(op.From, op.To); err != nil {
			rollback(completed)
			return fmt.Errorf("move %s -> %s: %w", op.From, op.To, err)
		}
		completed = append(completed, op)
		o.markSuppressed(op.From)
		o.markSuppressed(op.To)
	}
	if err := o.appendJournal("complete", *plan); err != nil {
		return err
	}
	return nil
}

func companionOperations(source, target string) ([]Operation, error) {
	ops := []Operation{{From: source, To: target}}
	if !media.Video(source) {
		return ops, nil
	}
	dir := filepath.Dir(source)
	base := strings.TrimSuffix(filepath.Base(source), filepath.Ext(source))
	targetBase := strings.TrimSuffix(filepath.Base(target), filepath.Ext(target))
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	allowed := map[string]bool{".srt": true, ".ass": true, ".ssa": true, ".sub": true, ".idx": true, ".nfo": true}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasPrefix(strings.ToLower(name), strings.ToLower(base)+".") {
			continue
		}
		if !allowed[strings.ToLower(filepath.Ext(name))] {
			continue
		}
		remainder := name[len(base):]
		ops = append(ops, Operation{
			From: filepath.Join(dir, name),
			To:   filepath.Join(filepath.Dir(target), targetBase+remainder),
		})
	}
	sort.SliceStable(ops[1:], func(i, j int) bool { return ops[i+1].From < ops[j+1].From })
	return ops, nil
}

func moveNoReplace(source, target string) error {
	if err := os.Link(source, target); err != nil {
		return err
	}
	if err := os.Remove(source); err != nil {
		_ = os.Remove(target)
		return err
	}
	return nil
}

func rollback(completed []Operation) {
	for i := len(completed) - 1; i >= 0; i-- {
		_ = moveNoReplace(completed[i].To, completed[i].From)
	}
}

func validateExistingTargetParent(root, target string) error {
	rootResolved, err := filepath.EvalSymlinks(root)
	if err != nil {
		return fmt.Errorf("resolve root: %w", err)
	}
	current := filepath.Dir(target)
	for {
		info, statErr := os.Lstat(current)
		if statErr == nil {
			if !info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
				return fmt.Errorf("target parent is not a directory: %s", current)
			}
			resolved, resolveErr := filepath.EvalSymlinks(current)
			if resolveErr != nil {
				return fmt.Errorf("resolve target parent: %w", resolveErr)
			}
			if !inside(rootResolved, resolved) && !samePath(rootResolved, resolved) {
				return fmt.Errorf("target parent resolves outside registered root: %s", current)
			}
			return nil
		}
		if !errors.Is(statErr, os.ErrNotExist) {
			return statErr
		}
		parent := filepath.Dir(current)
		if parent == current {
			return errors.New("unable to find existing target parent")
		}
		current = parent
	}
}

func (o *Organizer) ValidateRootAccess(root config.Root) error {
	info, err := os.Stat(root.Path)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return errors.New("root is not a directory")
	}
	f, err := os.Open(root.Path)
	if err != nil {
		return fmt.Errorf("root is not readable: %w", err)
	}
	_, readErr := f.Readdirnames(1)
	_ = f.Close()
	if readErr != nil && !errors.Is(readErr, io.EOF) {
		return fmt.Errorf("root is not listable: %w", readErr)
	}
	if root.Mode == config.Apply {
		test := filepath.Join(root.Path, fmt.Sprintf(".ocd-write-test-%d", os.Getpid()))
		if err := os.WriteFile(test, []byte("ocd permission probe\n"), 0o600); err != nil {
			return fmt.Errorf("apply mode requires write access: %w", err)
		}
		if err := os.Remove(test); err != nil {
			return fmt.Errorf("remove permission probe: %w", err)
		}
	}
	return nil
}

func (o *Organizer) Plans() []Plan {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return append([]Plan(nil), o.plans...)
}

func (o *Organizer) Findings() []Finding {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return append([]Finding(nil), o.findings...)
}

func (o *Organizer) record(plan Plan) Plan {
	o.mu.Lock()
	o.plans = append(o.plans, plan)
	if len(o.plans) > 1000 {
		o.plans = append([]Plan(nil), o.plans[len(o.plans)-1000:]...)
	}
	o.mu.Unlock()
	return plan
}

func (o *Organizer) addFinding(path, kind, detail string) {
	o.mu.Lock()
	o.findings = append(o.findings, Finding{At: time.Now().UTC(), Path: path, Kind: kind, Detail: detail})
	if len(o.findings) > 500 {
		o.findings = append([]Finding(nil), o.findings[len(o.findings)-500:]...)
	}
	o.mu.Unlock()
}

func (o *Organizer) appendJournal(stage string, plan Plan) error {
	if err := os.MkdirAll(o.StateDir, 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(o.StateDir, "journal.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if err := json.NewEncoder(f).Encode(map[string]any{"stage": stage, "at": time.Now().UTC(), "plan": plan}); err != nil {
		return err
	}
	return f.Sync()
}

func (o *Organizer) beginProcess(path string) bool {
	key := filepath.Clean(path)
	o.mu.Lock()
	defer o.mu.Unlock()
	if _, exists := o.inFlight[key]; exists {
		return false
	}
	o.inFlight[key] = struct{}{}
	return true
}

func (o *Organizer) endProcess(path string) {
	o.mu.Lock()
	delete(o.inFlight, filepath.Clean(path))
	o.mu.Unlock()
}

func (o *Organizer) markSuppressed(path string) {
	o.mu.Lock()
	o.recent[filepath.Clean(path)] = time.Now().Add(30 * time.Second)
	o.mu.Unlock()
}

func (o *Organizer) suppressed(path string) bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	now := time.Now()
	for key, until := range o.recent {
		if now.After(until) {
			delete(o.recent, key)
		}
	}
	until, ok := o.recent[filepath.Clean(path)]
	return ok && now.Before(until)
}

func planID(at time.Time, path string) string {
	sum := sha256.Sum256([]byte(at.UTC().Format(time.RFC3339Nano) + "\x00" + path))
	return hex.EncodeToString(sum[:8])
}

func inside(root, path string) bool {
	rel, err := filepath.Rel(filepath.Clean(root), filepath.Clean(path))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

func samePath(a, b string) bool {
	aa, _ := filepath.Abs(filepath.Clean(a))
	bb, _ := filepath.Abs(filepath.Clean(b))
	return aa == bb
}

// JournalLines is used by diagnostics without keeping an open database.
func JournalLines(path string, limit int) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var lines []string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		lines = append(lines, scanner.Text())
		if len(lines) > limit {
			lines = lines[len(lines)-limit:]
		}
	}
	return lines, scanner.Err()
}
