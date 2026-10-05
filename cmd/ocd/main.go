package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/thebrazenbeard/ocd/internal/buildinfo"
	"github.com/thebrazenbeard/ocd/internal/config"
	"github.com/thebrazenbeard/ocd/internal/organizer"
	"github.com/thebrazenbeard/ocd/internal/server"
	"github.com/thebrazenbeard/ocd/internal/watch"
)

func main() {
	if len(os.Args) < 2 {
		serve(os.Args[1:])
		return
	}
	switch os.Args[1] {
	case "serve":
		serve(os.Args[2:])
	case "root":
		rootCommand(os.Args[2:])
	case "tmdb":
		tmdbCommand(os.Args[2:])
	case "version":
		fmt.Printf("ocd %s\nsource_repository %s\nsource_revision %s\nsource_revision_url %s\n",
			buildinfo.Version, buildinfo.SourceURL, buildinfo.Revision, buildinfo.RevisionURL())
	default:
		log.Fatalf("unknown command %q; use serve, root, tmdb, or version", os.Args[1])
	}
}

func serve(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	listen := fs.String("listen", "127.0.0.1:9157", "HTTP listen address")
	stateDir := fs.String("state-dir", defaultStateDir(), "state directory")
	_ = fs.Parse(args)
	if !loopback(*listen) {
		log.Fatal("refusing non-loopback HTTP bind; v0.1 administration is loopback-only")
	}

	cfg, err := config.Open(filepath.Join(*stateDir, "config.json"))
	if err != nil {
		log.Fatal(err)
	}
	org := organizer.New(cfg, *stateDir)
	watcher, err := watch.New(cfg, org)
	if err != nil {
		log.Fatal(err)
	}
	defer watcher.Close()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	go watcher.Run(ctx)

	api := server.New(cfg, org)
	api.Reconcile = watcher.Reconcile
	httpServer := &http.Server{
		Addr: *listen, Handler: api.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       30 * time.Second,
	}
	go func() {
		log.Printf("OCD listening on http://%s", *listen)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("http: %v", err)
			cancel()
		}
	}()
	<-ctx.Done()
	shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	_ = httpServer.Shutdown(shutdown)
}

func rootCommand(args []string) {
	if len(args) == 0 {
		log.Fatal("root requires add, list, mode, or remove")
	}
	switch args[0] {
	case "add":
		fs := flag.NewFlagSet("root add", flag.ExitOnError)
		stateDir := fs.String("state-dir", defaultStateDir(), "state directory")
		path := fs.String("path", "", "absolute media root")
		mediaType := fs.String("type", "", "tv, movie, or music")
		mode := fs.String("mode", "observe", "observe or apply")
		providerName := fs.String("provider", "", "provider override")
		_ = fs.Parse(args[1:])
		cfg, err := config.Open(filepath.Join(*stateDir, "config.json"))
		if err != nil {
			log.Fatal(err)
		}
		root := config.Root{
			Path: *path, MediaType: config.MediaType(*mediaType),
			Mode: config.Mode(*mode), Provider: *providerName,
		}
		if root.Provider == "" {
			root.Provider = config.DefaultProvider(root.MediaType)
		}
		org := organizer.New(cfg, *stateDir)
		if err := org.ValidateRootAccess(root); err != nil {
			log.Fatal(err)
		}
		added, err := cfg.AddRoot(root)
		if err != nil {
			log.Fatal(err)
		}
		printJSON(added)
	case "list":
		fs := flag.NewFlagSet("root list", flag.ExitOnError)
		stateDir := fs.String("state-dir", defaultStateDir(), "state directory")
		_ = fs.Parse(args[1:])
		cfg, err := config.Open(filepath.Join(*stateDir, "config.json"))
		if err != nil {
			log.Fatal(err)
		}
		printJSON(cfg.Snapshot().Roots)
	case "mode":
		fs := flag.NewFlagSet("root mode", flag.ExitOnError)
		stateDir := fs.String("state-dir", defaultStateDir(), "state directory")
		id := fs.String("id", "", "root id")
		mode := fs.String("mode", "", "observe or apply")
		_ = fs.Parse(args[1:])
		cfg, err := config.Open(filepath.Join(*stateDir, "config.json"))
		if err != nil {
			log.Fatal(err)
		}
		var root *config.Root
		for _, candidate := range cfg.Snapshot().Roots {
			if candidate.ID == *id {
				copy := candidate
				root = &copy
				break
			}
		}
		if root == nil {
			log.Fatal("root not found")
		}
		root.Mode = config.Mode(*mode)
		org := organizer.New(cfg, *stateDir)
		if err := org.ValidateRootAccess(*root); err != nil {
			log.Fatal(err)
		}
		updated, err := cfg.UpdateRootMode(*id, config.Mode(*mode))
		if err != nil {
			log.Fatal(err)
		}
		printJSON(updated)
	case "remove":
		fs := flag.NewFlagSet("root remove", flag.ExitOnError)
		stateDir := fs.String("state-dir", defaultStateDir(), "state directory")
		id := fs.String("id", "", "root id")
		_ = fs.Parse(args[1:])
		cfg, err := config.Open(filepath.Join(*stateDir, "config.json"))
		if err != nil {
			log.Fatal(err)
		}
		if err := cfg.RemoveRoot(*id); err != nil {
			log.Fatal(err)
		}
	default:
		log.Fatalf("unknown root command %q", args[0])
	}
}

func tmdbCommand(args []string) {
	fs := flag.NewFlagSet("tmdb", flag.ExitOnError)
	stateDir := fs.String("state-dir", defaultStateDir(), "state directory")
	bearer := fs.String("bearer", "", "TMDB API Read Access Token")
	_ = fs.Parse(args)
	cfg, err := config.Open(filepath.Join(*stateDir, "config.json"))
	if err != nil {
		log.Fatal(err)
	}
	if err := cfg.SetTMDBBearer(*bearer); err != nil {
		log.Fatal(err)
	}
	fmt.Println("TMDB token updated")
}

func defaultStateDir() string {
	if value := os.Getenv("SYNOPKG_PKGVAR"); value != "" {
		return filepath.Join(value, "state")
	}
	if value := os.Getenv("OCD_STATE_DIR"); value != "" {
		return value
	}
	return "./ocd-state"
}

func loopback(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func printJSON(value any) {
	data, err := jsonMarshalIndent(value)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(string(data))
}

func jsonMarshalIndent(value any) ([]byte, error) {
	return json.MarshalIndent(value, "", "  ")
}
