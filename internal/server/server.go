package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/thebrazenbeard/ocd/internal/buildinfo"
	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/organizer"
)

type Server struct {
	Config    *config.Store
	Organizer *organizer.Organizer
	Reconcile func(context.Context)

	started time.Time
}

func New(cfg *config.Store, org *organizer.Organizer) *Server {
	return &Server{Config: cfg, Organizer: org, started: time.Now().UTC()}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /api/v1/status", s.status)
	mux.HandleFunc("GET /api/v1/roots", s.roots)
	mux.HandleFunc("POST /api/v1/roots", s.addRoot)
	mux.HandleFunc("DELETE /api/v1/roots/{id}", s.deleteRoot)
	mux.HandleFunc("PUT /api/v1/roots/{id}/mode", s.setRootMode)
	mux.HandleFunc("GET /api/v1/plans", s.plans)
	mux.HandleFunc("GET /api/v1/findings", s.findings)
	mux.HandleFunc("PUT /api/v1/settings/tmdb", s.setTMDB)
	mux.HandleFunc("POST /api/v1/reconcile", s.reconcile)
	mux.HandleFunc("GET /", s.index)
	return requestLogging(mux)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s *Server) status(w http.ResponseWriter, _ *http.Request) {
	cfg := s.Config.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"schema_version":    1,
		"version":           buildinfo.Version,
		"started_at":        s.started,
		"uptime_seconds":    int(time.Since(s.started).Seconds()),
		"roots":             len(cfg.Roots),
		"plans":             len(s.Organizer.Plans()),
		"findings":          len(s.Organizer.Findings()),
		"tmdb_configured":   strings.TrimSpace(cfg.TMDBBearer) != "",
		"settle_seconds":    cfg.SettleSeconds,
		"reconcile_minutes": cfg.ReconcileMinute,
	})
}

func (s *Server) roots(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.Config.Snapshot().Roots)
}

func (s *Server) addRoot(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Path      string           `json:"path"`
		MediaType config.MediaType `json:"media_type"`
		Provider  string           `json:"provider,omitempty"`
		Mode      config.Mode      `json:"mode"`
	}
	if err := decodeJSON(r, &input); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	root := config.Root{
		Path: input.Path, MediaType: input.MediaType,
		Provider: input.Provider, Mode: input.Mode,
	}
	if root.Provider == "" {
		root.Provider = config.DefaultProvider(root.MediaType)
	}
	if root.Mode == "" {
		root.Mode = config.Observe
	}
	if err := s.Organizer.ValidateRootAccess(root); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	added, err := s.Config.AddRoot(root)
	if err != nil {
		writeError(w, http.StatusConflict, err)
		return
	}
	writeJSON(w, http.StatusCreated, added)
}

func (s *Server) deleteRoot(w http.ResponseWriter, r *http.Request) {
	if err := s.Config.RemoveRoot(r.PathValue("id")); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeError(w, http.StatusNotFound, err)
		} else {
			writeError(w, http.StatusInternalServerError, err)
		}
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) setRootMode(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Mode config.Mode `json:"mode"`
	}
	if err := decodeJSON(r, &input); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	var root *config.Root
	for _, candidate := range s.Config.Snapshot().Roots {
		if candidate.ID == r.PathValue("id") {
			copy := candidate
			root = &copy
			break
		}
	}
	if root == nil {
		writeError(w, http.StatusNotFound, os.ErrNotExist)
		return
	}
	root.Mode = input.Mode
	if err := s.Organizer.ValidateRootAccess(*root); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	updated, err := s.Config.UpdateRootMode(root.ID, input.Mode)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Server) plans(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.Organizer.Plans())
}

func (s *Server) findings(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.Organizer.Findings())
}

func (s *Server) setTMDB(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Bearer string `json:"bearer"`
	}
	if err := decodeJSON(r, &input); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if err := s.Config.SetTMDBBearer(input.Bearer); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"tmdb_configured": strings.TrimSpace(input.Bearer) != ""})
}

func (s *Server) reconcile(w http.ResponseWriter, _ *http.Request) {
	if s.Reconcile == nil {
		writeError(w, http.StatusNotImplemented, errors.New("reconcile callback unavailable"))
		return
	}
	go s.Reconcile(context.Background())
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "scheduled"})
}

func (s *Server) index(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(indexHTML)
}

func decodeJSON(r *http.Request, out any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return err
	}
	var extra any
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("request body must contain exactly one JSON value")
		}
		return err
	}
	return nil
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func requestLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		if r.URL.Path != "/healthz" {
			fmt.Printf("%s %s %s\n", r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
		}
	})
}

var indexHTML = []byte(`<!doctype html>
<html><head><meta charset="utf-8"><title>OCD</title>
<style>
body{font:14px system-ui;margin:24px;max-width:1100px;background:#111;color:#eee}
h1{margin-bottom:4px}.muted{color:#aaa}.card{background:#1d1d1d;padding:14px;margin:12px 0;border-radius:8px}
input,select,button{font:inherit;padding:7px;margin:4px;background:#222;color:#eee;border:1px solid #555;border-radius:4px}
input[type=text],input[type=password]{min-width:300px}button{cursor:pointer}
table{border-collapse:collapse;width:100%}th,td{padding:7px;border-bottom:1px solid #333;text-align:left}
.ok{color:#8f8}.held{color:#fd8}.error{color:#f88}code{color:#9fe}
</style></head><body>
<h1>OCD</h1><div class="muted">Organized &amp; Clean DiskStation</div>
<div class="card"><b>Root contract:</b> every watched folder is explicitly Television, Movies, or Music. OCD never guesses.</div>
<div class="card">
<h2>Add media root</h2>
<input id="path" type="text" placeholder="/volume1/TV Shows">
<select id="type"><option value="tv">Television</option><option value="movie">Movies</option><option value="music">Music</option></select>
<select id="mode"><option value="observe">Observe only</option><option value="apply">Apply renames</option></select>
<button onclick="addRoot()">Add</button>
<div class="muted">TV → Show/Season/SxxExx - Episode Name · Movies → Title (Year) · Music → Artist/Album/Song Title</div>
</div>
<div class="card">
<h2>TMDB movie provider</h2>
<input id="tmdb" type="password" placeholder="TMDB API Read Access Token">
<button onclick="setTMDB()">Save token</button>
<span class="muted">Required only for movie metadata.</span>
</div>
<div class="card"><h2>Roots</h2><div id="roots">Loading…</div></div>
<div class="card"><h2>Recent plans</h2><div id="plans">Loading…</div></div>
<div class="card"><h2>Findings</h2><div id="findings">Loading…</div></div>
<script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opt){const r=await fetch(url,opt);const t=await r.text();let x={};try{x=JSON.parse(t)}catch{}if(!r.ok)throw Error(x.error||t||r.statusText);return x}
async function addRoot(){try{await api('/api/v1/roots',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({path:path.value,media_type:type.value,mode:mode.value})});path.value='';await tick()}catch(e){alert(e)}}
async function setTMDB(){try{await api('/api/v1/settings/tmdb',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({bearer:tmdb.value})});tmdb.value='';alert('Saved')}catch(e){alert(e)}}
async function del(id){if(confirm('Remove this OCD root registration? Files are not deleted.')){await api('/api/v1/roots/'+encodeURIComponent(id),{method:'DELETE'});await tick()}}
async function setMode(id,mode){try{await api('/api/v1/roots/'+encodeURIComponent(id)+'/mode',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({mode})});await tick()}catch(e){alert(e)}}
async function tick(){
 const rs=await api('/api/v1/roots'); const ps=await api('/api/v1/plans'); const fs=await api('/api/v1/findings');
 roots.innerHTML=rs.length?'<table><tr><th>Path</th><th>Type</th><th>Provider</th><th>Mode</th><th></th></tr>'+rs.map(r=>'<tr><td><code>'+esc(r.path)+'</code></td><td>'+esc(r.media_type)+'</td><td>'+esc(r.provider)+'</td><td>'+esc(r.mode)+'</td><td><button onclick="setMode(\''+esc(r.id)+'\',\''+(r.mode==='apply'?'observe':'apply')+'\')">'+(r.mode==='apply'?'Observe':'Apply')+'</button><button onclick="del(\''+esc(r.id)+'\')">Remove</button></td></tr>').join('')+'</table>':'No roots registered';
 plans.innerHTML=ps.length?'<table><tr><th>Status</th><th>Source</th><th>Target / Reason</th></tr>'+ps.slice(-50).reverse().map(p=>'<tr><td class="'+esc(p.status)+'">'+esc(p.status)+'</td><td><code>'+esc(p.source)+'</code></td><td>'+esc(p.target||p.reason)+'</td></tr>').join('')+'</table>':'No plans yet';
 findings.innerHTML=fs.length?fs.slice(-50).reverse().map(f=>'<div class="held">'+esc(f.kind)+': '+esc(f.path)+' — '+esc(f.detail)+'</div>').join(''):'No findings';
}
tick();setInterval(tick,5000);
</script></body></html>`)
