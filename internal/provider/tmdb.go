package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"unicode"

	"github.com/thebrazenbeard/ocd/internal/media"
)

type TMDB struct {
	Client *http.Client
	Bearer string
}

type tmdbSearchResponse struct {
	Results []struct {
		ID          int     `json:"id"`
		Title       string  `json:"title"`
		ReleaseDate string  `json:"release_date"`
		Popularity  float64 `json:"popularity"`
	} `json:"results"`
}

func (t TMDB) Resolve(ctx context.Context, hint media.MovieHint) (MovieResult, error) {
	token := strings.TrimSpace(t.Bearer)
	if token == "" {
		return MovieResult{}, ErrCredentials
	}
	client := t.Client
	if client == nil {
		client = &http.Client{Timeout: HTTPTimeout()}
	}
	q := url.Values{}
	q.Set("query", hint.Title)
	q.Set("include_adult", "false")
	if hint.Year > 0 {
		q.Set("year", strconv.Itoa(hint.Year))
	}
	endpoint := "https://api.themoviedb.org/3/search/movie?" + q.Encode()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "OCD/0.1 (+https://github.com/thebrazenbeard/ocd)")
	resp, err := client.Do(req)
	if err != nil {
		return MovieResult{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized {
		return MovieResult{}, ErrCredentials
	}
	if resp.StatusCode != http.StatusOK {
		return MovieResult{}, fmt.Errorf("tmdb search: %s", resp.Status)
	}
	var payload tmdbSearchResponse
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return MovieResult{}, err
	}
	if len(payload.Results) == 0 {
		return MovieResult{}, ErrNoMatch
	}
	bestScore := -1.0
	second := -1.0
	bestIndex := -1
	for i, item := range payload.Results {
		year := releaseYear(item.ReleaseDate)
		score := titleScore(hint.Title, item.Title)
		if hint.Year > 0 {
			if year == hint.Year {
				score += 0.15
			} else if year > 0 {
				score -= 0.15
			}
		}
		if score > bestScore {
			second = bestScore
			bestScore = score
			bestIndex = i
		} else if score > second {
			second = score
		}
	}
	if bestIndex < 0 || bestScore < 0.78 {
		return MovieResult{}, ErrNoMatch
	}
	if second >= bestScore-0.05 {
		return MovieResult{}, ErrAmbiguous
	}
	best := payload.Results[bestIndex]
	return MovieResult{
		Title: best.Title, Year: releaseYear(best.ReleaseDate), Score: bestScore,
		ProviderID: strconv.Itoa(best.ID),
	}, nil
}

func releaseYear(value string) int {
	if len(value) < 4 {
		return 0
	}
	year, _ := strconv.Atoi(value[:4])
	return year
}

func titleScore(a, b string) float64 {
	aa := tokenSet(a)
	bb := tokenSet(b)
	if len(aa) == 0 || len(bb) == 0 {
		return 0
	}
	intersection := 0
	for token := range aa {
		if _, ok := bb[token]; ok {
			intersection++
		}
	}
	union := len(aa) + len(bb) - intersection
	score := float64(intersection) / float64(union)
	if normalizeTitle(a) == normalizeTitle(b) {
		score = 1
	}
	return score
}

func tokenSet(value string) map[string]struct{} {
	out := map[string]struct{}{}
	for _, token := range strings.Fields(normalizeTitle(value)) {
		out[token] = struct{}{}
	}
	return out
}

func normalizeTitle(value string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(value) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
		} else {
			b.WriteByte(' ')
		}
	}
	return strings.Join(strings.Fields(b.String()), " ")
}
