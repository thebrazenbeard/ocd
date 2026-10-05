package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/thebrazenbeard/ocd/internal/media"
)

type TVMaze struct {
	Client *http.Client
}

type tvMazeSearch struct {
	Score float64 `json:"score"`
	Show  struct {
		ID        int    `json:"id"`
		Name      string `json:"name"`
		Premiered string `json:"premiered"`
	} `json:"show"`
}

type tvMazeEpisode struct {
	ID     int    `json:"id"`
	Name   string `json:"name"`
	Season int    `json:"season"`
	Number int    `json:"number"`
}

func (t TVMaze) Resolve(ctx context.Context, hint media.TVHint) (TVResult, error) {
	client := t.Client
	if client == nil {
		client = &http.Client{Timeout: HTTPTimeout()}
	}
	endpoint := "https://api.tvmaze.com/search/shows?q=" + url.QueryEscape(hint.Show)
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	req.Header.Set("User-Agent", "OCD/0.1 (+https://github.com/thebrazenbeard/ocd)")
	resp, err := client.Do(req)
	if err != nil {
		return TVResult{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return TVResult{}, fmt.Errorf("tvmaze search: %s", resp.Status)
	}
	var matches []tvMazeSearch
	if err := json.NewDecoder(resp.Body).Decode(&matches); err != nil {
		return TVResult{}, err
	}
	if len(matches) == 0 || matches[0].Score < 0.60 {
		return TVResult{}, ErrNoMatch
	}
	if len(matches) > 1 && matches[1].Score >= matches[0].Score-0.08 {
		if !strings.EqualFold(matches[0].Show.Name, hint.Show) {
			return TVResult{}, ErrAmbiguous
		}
	}
	top := matches[0]
	epURL := fmt.Sprintf(
		"https://api.tvmaze.com/shows/%d/episodebynumber?season=%d&number=%d",
		top.Show.ID, hint.Season, hint.Episode,
	)
	req, _ = http.NewRequestWithContext(ctx, http.MethodGet, epURL, nil)
	req.Header.Set("User-Agent", "OCD/0.1 (+https://github.com/thebrazenbeard/ocd)")
	resp, err = client.Do(req)
	if err != nil {
		return TVResult{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return TVResult{}, ErrNoMatch
	}
	if resp.StatusCode != http.StatusOK {
		return TVResult{}, fmt.Errorf("tvmaze episode: %s", resp.Status)
	}
	var ep tvMazeEpisode
	if err := json.NewDecoder(resp.Body).Decode(&ep); err != nil {
		return TVResult{}, err
	}
	year := 0
	if len(top.Show.Premiered) >= 4 {
		year, _ = strconv.Atoi(top.Show.Premiered[:4])
	}
	return TVResult{
		ShowName: top.Show.Name, ShowYear: year, EpisodeName: ep.Name,
		Score: top.Score, ProviderID: strconv.Itoa(top.Show.ID),
	}, nil
}
