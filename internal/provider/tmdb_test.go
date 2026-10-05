package provider

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/thebrazenbeard/ocd/internal/media"
)

func TestTMDBResolveRequiresCredential(t *testing.T) {
	_, err := (TMDB{}).Resolve(context.Background(), media.MovieHint{Title: "Arrival", Year: 2016})
	if err != ErrCredentials {
		t.Fatalf("got %v", err)
	}
}

func TestTMDBResolve(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Header.Get("Authorization") != "Bearer token" {
			t.Fatal("missing bearer")
		}
		return jsonResponse(`{"results":[{"id":329865,"title":"Arrival","release_date":"2016-11-10","popularity":20}]}`), nil
	})}
	got, err := (TMDB{Client: client, Bearer: "token"}).Resolve(context.Background(), media.MovieHint{Title: "Arrival", Year: 2016})
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(got.Title) != "Arrival" || got.Year != 2016 {
		t.Fatalf("unexpected result: %+v", got)
	}
}
