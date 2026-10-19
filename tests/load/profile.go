package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
)

// Classification of a profile: the ENV-05 arrival rates of the target
// environment, or development rates that size nothing (platform.md capacity
// worksheet stays unfilled from them).
const (
	ClassDevelopment = "DEVELOPMENT_ONLY"
	ClassEnv05       = "ENV-05"
)

// Profile is one load profile (profiles/*.json). Unknown fields are refused so
// a misspelled rate never silently runs at zero.
type Profile struct {
	ProfileID       string `json:"profileId"`
	Classification  string `json:"classification"`
	Source          string `json:"source,omitempty"`
	DurationSeconds int    `json:"durationSeconds"`
	// DrainSeconds bounds the wait for in-flight operations after the last
	// arrival; an operation still open then counts as timed out.
	DrainSeconds int `json:"drainSeconds"`
	// MaxErrorRatio is the share of failed requests (5xx, transport errors)
	// per scenario above which the run is FAIL.
	MaxErrorRatio float64             `json:"maxErrorRatio"`
	Scenarios     map[string]Scenario `json:"scenarios"`
}

// Scenario is the open-model arrival rate of one scenario and its bounds.
type Scenario struct {
	RatePerSecond float64 `json:"ratePerSecond"`
	MaxInFlight   int     `json:"maxInFlight"`
	// LocalCheck and Preparation operations: the share canceled through the
	// commands endpoint after their first progress; the share held there and
	// resumed holdSeconds later (an operator's hold under load); whether the
	// subscriber drops and resumes its stream (Last-Event-ID) once; extra
	// subscribers of the same stream; the bound on one operation.
	CancelRatio     float64 `json:"cancelRatio,omitempty"`
	HoldRatio       float64 `json:"holdRatio,omitempty"`
	HoldSeconds     int     `json:"holdSeconds,omitempty"`
	ResumeStream    bool    `json:"resumeStream,omitempty"`
	ExtraListeners  int     `json:"extraListeners,omitempty"`
	DeadlineSeconds int     `json:"deadlineSeconds,omitempty"`
	// Knowledge searches.
	Query           string `json:"query,omitempty"`
	MaxContextItems int    `json:"maxContextItems,omitempty"`
}

// Scenario names the tool drives through the public API.
var knownScenarios = map[string]bool{"local_check": true, "preparation": true, "knowledge_search": true, "mcp_catalog": true}

func loadProfile(path string) (Profile, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Profile{}, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var p Profile
	if err := dec.Decode(&p); err != nil {
		return Profile{}, fmt.Errorf("%s: %w", path, err)
	}
	return p, p.validate()
}

func (p Profile) validate() error {
	switch p.Classification {
	case ClassDevelopment:
	case ClassEnv05:
		if p.Source == "" {
			return fmt.Errorf("profile %s: an ENV-05 profile names its source (the approved ENV-05 record)", p.ProfileID)
		}
	default:
		return fmt.Errorf("profile %s: classification %q is neither %s nor %s", p.ProfileID, p.Classification, ClassDevelopment, ClassEnv05)
	}
	if p.ProfileID == "" || p.DurationSeconds <= 0 || p.DrainSeconds < 0 || p.MaxErrorRatio < 0 || p.MaxErrorRatio > 1 {
		return fmt.Errorf("profile %q: profileId, durationSeconds > 0, drainSeconds >= 0 and maxErrorRatio in [0,1] are required", p.ProfileID)
	}
	if len(p.Scenarios) == 0 {
		return fmt.Errorf("profile %s: no scenarios", p.ProfileID)
	}
	for name, s := range p.Scenarios {
		if !knownScenarios[name] {
			return fmt.Errorf("profile %s: unknown scenario %q", p.ProfileID, name)
		}
		if s.RatePerSecond <= 0 || s.MaxInFlight <= 0 || s.CancelRatio < 0 || s.CancelRatio > 1 || s.HoldRatio < 0 || s.HoldRatio > 1 ||
			s.CancelRatio+s.HoldRatio > 1 || s.HoldSeconds < 0 || s.ExtraListeners < 0 {
			return fmt.Errorf("profile %s: scenario %s needs ratePerSecond > 0, maxInFlight > 0, cancelRatio and holdRatio in [0,1] with a sum <= 1", p.ProfileID, name)
		}
	}
	return nil
}
