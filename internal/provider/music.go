package provider

import (
	"fmt"
	"os"
	"strings"

	"github.com/dhowden/tag"
)

type MusicResult struct {
	Artist string `json:"artist"`
	Album  string `json:"album"`
	Title  string `json:"title"`
	Track  int    `json:"track,omitempty"`
	Disc   int    `json:"disc,omitempty"`
	Year   int    `json:"year,omitempty"`
}

func ReadMusic(path string) (MusicResult, error) {
	f, err := os.Open(path)
	if err != nil {
		return MusicResult{}, err
	}
	defer f.Close()
	meta, err := tag.ReadFrom(f)
	if err != nil {
		return MusicResult{}, fmt.Errorf("read embedded tags: %w", err)
	}
	track, _ := meta.Track()
	disc, _ := meta.Disc()
	result := MusicResult{
		Artist: strings.TrimSpace(meta.Artist()),
		Album:  strings.TrimSpace(meta.Album()),
		Title:  strings.TrimSpace(meta.Title()),
		Track:  track,
		Disc:   disc,
		Year:   meta.Year(),
	}
	if result.Artist == "" || result.Album == "" || result.Title == "" {
		return MusicResult{}, fmt.Errorf("music file requires embedded artist, album, and title")
	}
	return result, nil
}
