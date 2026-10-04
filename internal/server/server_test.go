package server

import (
	"bytes"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

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
