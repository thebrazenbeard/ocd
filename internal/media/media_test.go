package media

import (
	"path/filepath"
	"testing"
)

func TestTVNamingContract(t *testing.T) {
	hint, err := ParseTV("The.Show.S02E07.1080p.WEB-DL.mkv")
	if err != nil {
		t.Fatal(err)
	}
	if hint.Show != "The Show" || hint.Season != 2 || hint.Episode != 7 {
		t.Fatalf("unexpected hint: %+v", hint)
	}
	got := filepath.ToSlash(TVTarget("/media/tv", "The Show", 2, 7, "The Episode", ".mkv"))
	want := "/media/tv/The Show/Season 02/S02E07 - The Episode.mkv"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestMovieRequiresYear(t *testing.T) {
	if _, err := ParseMovie("Arrival.1080p.BluRay.mkv"); err == nil {
		t.Fatal("expected movie without release year to be held")
	}
}

func TestMovieNamingContract(t *testing.T) {
	hint, err := ParseMovie("Arrival.2016.1080p.BluRay.x265.mkv")
	if err != nil {
		t.Fatal(err)
	}
	if hint.Title != "Arrival" || hint.Year != 2016 {
		t.Fatalf("unexpected hint: %+v", hint)
	}
	got := filepath.ToSlash(MovieTarget("/media/movies", "Arrival", 2016, ".mkv"))
	if got != "/media/movies/Arrival (2016)/Arrival (2016).mkv" {
		t.Fatalf("unexpected target %q", got)
	}
}

func TestMusicNamingContractHasNoTrackNumber(t *testing.T) {
	got := filepath.ToSlash(MusicTarget("/media/music", "Massive Attack", "Mezzanine", "Teardrop", ".flac"))
	if got != "/media/music/Massive Attack/Mezzanine/Teardrop.flac" {
		t.Fatalf("unexpected target %q", got)
	}
}

func TestCanonicalFastPaths(t *testing.T) {
	tv := filepath.Join("C:", "media", "Show", "Season 01", "S01E02 - Episode.mkv")
	if !LooksCanonicalTV(filepath.Join("C:", "media"), tv) {
		t.Fatal("expected canonical TV path")
	}
	movie := filepath.Join("C:", "movies", "Arrival (2016)", "Arrival (2016).mkv")
	if !LooksCanonicalMovie(filepath.Join("C:", "movies"), movie) {
		t.Fatal("expected canonical movie path")
	}
}

func TestSafeComponent(t *testing.T) {
	if got := SafeComponent(`A:B?  C* `); got != "A-B- C-" {
		t.Fatalf("unexpected component %q", got)
	}
}
