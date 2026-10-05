package provider

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/thebrazenbeard/ocd/internal/media"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func jsonResponse(body string) *http.Response {
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func TestTVMazeResolve(t *testing.T) {
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		switch {
		case strings.Contains(r.URL.Path, "/search/shows"):
			return jsonResponse(`[{"score":0.99,"show":{"id":42,"name":"Example Show","premiered":"2020-01-01"}}]`), nil
		case strings.Contains(r.URL.Path, "/episodebynumber"):
			return jsonResponse(`{"id":99,"name":"Pilot","season":1,"number":1}`), nil
		default:
			return &http.Response{
				StatusCode: http.StatusNotFound,
				Status:     "404 Not Found",
				Header:     make(http.Header),
				Body:       io.NopCloser(strings.NewReader("")),
			}, nil
		}
	})}
	got, err := (TVMaze{Client: client}).Resolve(context.Background(), media.TVHint{Show: "Example Show", Season: 1, Episode: 1})
	if err != nil {
		t.Fatal(err)
	}
	if got.ShowName != "Example Show" || got.EpisodeName != "Pilot" || got.ProviderID != "42" {
		t.Fatalf("unexpected result: %+v", got)
	}
}
