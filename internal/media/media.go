package media

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode"
)

type TVHint struct {
	Show    string
	Season  int
	Episode int
}

type MovieHint struct {
	Title string
	Year  int
}

var (
	tvPattern       = regexp.MustCompile(`(?i)^(.+?)[ ._-]+s(\d{1,2})e(\d{1,3})(?:\D|$)`)
	tvCanonical     = regexp.MustCompile(`(?i)^s(\d{2})e(\d{2,3}) - .+`)
	seasonCanonical = regexp.MustCompile(`(?i)^Season (\d{2})$`)
	movieCanonical  = regexp.MustCompile(`^.+ \((?:19|20)\d{2}\)$`)
	yearPattern     = regexp.MustCompile(`(?:^|[ ._\-(])((?:19|20)\d{2})(?:[ ._)\-]|$)`)
	junkPattern     = regexp.MustCompile(`(?i)\b(2160p|1080p|720p|480p|x264|x265|h264|h265|hevc|web[-_. ]?dl|webrip|bluray|brrip|dvdrip|hdr|remux|aac|dts|proper|repack)\b.*$`)
	spacePattern    = regexp.MustCompile(`\s+`)
)

func ParseTV(path string) (TVHint, error) {
	stem := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	m := tvPattern.FindStringSubmatch(stem)
	if len(m) != 4 {
		return TVHint{}, fmt.Errorf("no SxxExx token")
	}
	season, _ := strconv.Atoi(m[2])
	episode, _ := strconv.Atoi(m[3])
	show := CleanTitle(m[1])
	if show == "" {
		return TVHint{}, fmt.Errorf("empty show title")
	}
	return TVHint{Show: show, Season: season, Episode: episode}, nil
}

func ParseMovie(path string) (MovieHint, error) {
	stem := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	stem = junkPattern.ReplaceAllString(stem, "")
	match := yearPattern.FindStringSubmatchIndex(stem)
	if match != nil {
		year, _ := strconv.Atoi(stem[match[2]:match[3]])
		title := CleanTitle(stem[:match[0]])
		if title != "" {
			return MovieHint{Title: title, Year: year}, nil
		}
	}
	title := CleanTitle(stem)
	if title == "" {
		return MovieHint{}, fmt.Errorf("empty movie title")
	}
	return MovieHint{}, fmt.Errorf("movie filename must include a four-digit release year: %s", title)
}

func CleanTitle(value string) string {
	value = strings.ReplaceAll(value, ".", " ")
	value = strings.ReplaceAll(value, "_", " ")
	value = strings.Trim(value, " -._()[]{}")
	return spacePattern.ReplaceAllString(strings.TrimSpace(value), " ")
}

func SafeComponent(value string) string {
	value = strings.TrimSpace(value)
	var b strings.Builder
	lastSpace := false
	for _, r := range value {
		switch r {
		case '<', '>', ':', '"', '/', '\\', '|', '?', '*':
			r = '-'
		}
		if unicode.IsControl(r) {
			continue
		}
		if unicode.IsSpace(r) {
			if lastSpace {
				continue
			}
			r = ' '
			lastSpace = true
		} else {
			lastSpace = false
		}
		b.WriteRune(r)
	}
	out := strings.TrimSpace(b.String())
	out = strings.TrimRight(out, ". ")
	if out == "" {
		return "Unknown"
	}
	return out
}

func TVTarget(root, show string, season, episode int, episodeName, ext string) string {
	show = SafeComponent(show)
	episodeName = SafeComponent(episodeName)
	name := fmt.Sprintf("S%02dE%02d - %s%s", season, episode, episodeName, ext)
	return filepath.Join(root, show, fmt.Sprintf("Season %02d", season), name)
}

func MovieTarget(root, title string, year int, ext string) string {
	title = SafeComponent(title)
	label := title
	if year > 0 {
		label = fmt.Sprintf("%s (%d)", title, year)
	}
	return filepath.Join(root, label, label+ext)
}

func MusicTarget(root, artist, album, title, ext string) string {
	return filepath.Join(root, SafeComponent(artist), SafeComponent(album), SafeComponent(title)+ext)
}

func Supported(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts",
		".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma":
		return true
	default:
		return false
	}
}

func Video(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts":
		return true
	default:
		return false
	}
}

func Audio(path string) bool { return Supported(path) && !Video(path) }

func LooksCanonicalTV(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	if err != nil || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	parts := strings.Split(filepath.Clean(rel), string(filepath.Separator))
	if len(parts) != 3 || parts[0] == "" {
		return false
	}
	season := seasonCanonical.FindStringSubmatch(parts[1])
	episode := tvCanonical.FindStringSubmatch(strings.TrimSuffix(parts[2], filepath.Ext(parts[2])))
	return len(season) == 2 && len(episode) == 3 && season[1] == episode[1]
}

func LooksCanonicalMovie(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	if err != nil || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	parts := strings.Split(filepath.Clean(rel), string(filepath.Separator))
	if len(parts) != 2 {
		return false
	}
	stem := strings.TrimSuffix(parts[1], filepath.Ext(parts[1]))
	return movieCanonical.MatchString(parts[0]) && parts[0] == stem
}
