package buildinfo

var Version = "dev"
var Revision = "unknown"
var SourceURL = "https://github.com/thebrazenbeard/ocd"

func RevisionURL() string {
	if Revision == "" || Revision == "unknown" || Revision == "dev" {
		return SourceURL
	}
	return SourceURL + "/commit/" + Revision
}
