package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/thebrazenbeard/ocd/internal/buildinfo"
	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/organizer"
)

type recorder struct {
	header http.Header
	code   int
	body   bytes.Buffer
}

func newRecorder() *recorder {
	return &recorder{header: make(http.Header), code: http.StatusOK}
}

func (r *recorder) Header() http.Header         { return r.header }
func (r *recorder) WriteHeader(code int)        { r.code = code }
func (r *recorder) Write(p []byte) (int, error) { return r.body.Write(p) }

func testServer(t *testing.T) (*Server, string) {
	t.Helper()
	state := t.TempDir()
	cfg, err := config.Open(filepath.Join(state, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	return New(cfg, organizer.New(cfg, state)), state
}

func TestRootAPIRequiresExplicitTypeAndCanPromoteMode(t *testing.T) {
	srv, _ := testServer(t)
	root := t.TempDir()

	req, err := http.NewRequest(
		http.MethodPost,
		"http://localhost/api/v1/roots",
		strings.NewReader(`{"path":"`+strings.ReplaceAll(root, "\\", "\\\\")+`","media_type":"tv","mode":"observe"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.code != http.StatusCreated {
		t.Fatalf("add root: %d %s", rec.code, rec.body.String())
	}
	roots := srv.Config.Snapshot().Roots
	if len(roots) != 1 || roots[0].MediaType != config.TV || roots[0].Mode != config.Observe {
		t.Fatalf("unexpected roots: %+v", roots)
	}

	modeReq, err := http.NewRequest(
		http.MethodPut,
		"http://localhost/api/v1/roots/"+roots[0].ID+"/mode",
		strings.NewReader(`{"mode":"apply"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	modeRec := newRecorder()
	srv.Handler().ServeHTTP(modeRec, modeReq)
	if modeRec.code != http.StatusOK {
		t.Fatalf("set mode: %d %s", modeRec.code, modeRec.body.String())
	}
	if got := srv.Config.Snapshot().Roots[0].Mode; got != config.Apply {
		t.Fatalf("mode=%q", got)
	}
}

func TestDecodeJSONRejectsTrailingValue(t *testing.T) {
	srv, _ := testServer(t)
	root := strings.ReplaceAll(t.TempDir(), "\\", "\\\\")
	req, err := http.NewRequest(
		http.MethodPost,
		"http://localhost/api/v1/roots",
		strings.NewReader(`{"path":"`+root+`","media_type":"tv","mode":"observe"} {}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.code != http.StatusBadRequest {
		t.Fatalf("code=%d body=%s", rec.code, rec.body.String())
	}
}

func TestIndexExplainsMultiRootManagement(t *testing.T) {
	srv, _ := testServer(t)
	req, err := http.NewRequest(http.MethodGet, "http://localhost/", nil)
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.code != http.StatusOK {
		t.Fatalf("index: %d %s", rec.code, rec.body.String())
	}
	body := rec.body.String()
	for _, want := range []string{"Add as many media roots as you need", "DSM ACL", "sc-OCD"} {
		if !strings.Contains(body, want) {
			t.Fatalf("index missing %q", want)
		}
	}
}

func TestStatusReportsSourceProvenance(t *testing.T) {
	oldVersion, oldRevision, oldSourceURL := buildinfo.Version, buildinfo.Revision, buildinfo.SourceURL
	buildinfo.Version = "0.1.0-test"
	buildinfo.Revision = "0123456789abcdef0123456789abcdef01234567"
	buildinfo.SourceURL = "https://github.com/thebrazenbeard/ocd"
	t.Cleanup(func() {
		buildinfo.Version, buildinfo.Revision, buildinfo.SourceURL = oldVersion, oldRevision, oldSourceURL
	})

	srv, _ := testServer(t)
	req, err := http.NewRequest(http.MethodGet, "http://localhost/api/v1/status", nil)
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.code != http.StatusOK {
		t.Fatalf("status: %d %s", rec.code, rec.body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["source_repository"] != buildinfo.SourceURL {
		t.Fatalf("source_repository=%v", body["source_repository"])
	}
	if body["source_revision"] != buildinfo.Revision {
		t.Fatalf("source_revision=%v", body["source_revision"])
	}
	if body["source_revision_url"] != buildinfo.SourceURL+"/commit/"+buildinfo.Revision {
		t.Fatalf("source_revision_url=%v", body["source_revision_url"])
	}
}
