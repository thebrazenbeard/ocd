package provider

import (
	"context"
	"errors"
	"time"

	"github.com/thebrazenbeard/ocd/internal/media"
)

var (
	ErrAmbiguous   = errors.New("metadata match is ambiguous")
	ErrNoMatch     = errors.New("metadata match not found")
	ErrCredentials = errors.New("metadata provider credentials are missing")
)

type TVResult struct {
	ShowName    string  `json:"show_name"`
	ShowYear    int     `json:"show_year,omitempty"`
	EpisodeName string  `json:"episode_name"`
	Score       float64 `json:"score"`
	ProviderID  string  `json:"provider_id"`
}

type MovieResult struct {
	Title      string  `json:"title"`
	Year       int     `json:"year"`
	Score      float64 `json:"score"`
	ProviderID string  `json:"provider_id"`
}

type TV interface {
	Resolve(context.Context, media.TVHint) (TVResult, error)
}

type Movie interface {
	Resolve(context.Context, media.MovieHint) (MovieResult, error)
}

func HTTPTimeout() time.Duration { return 8 * time.Second }
