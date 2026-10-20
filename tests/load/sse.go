package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// frame is one SSE event of API-04.
type frame struct {
	ID    string
	Event string
	Data  map[string]any
}

func (f frame) lifecycle() string {
	p, _ := f.Data["payload"].(map[string]any)
	l, _ := p["lifecycle"].(string)
	return l
}

func (f frame) terminal() bool {
	switch f.lifecycle() {
	case "succeeded", "failed", "canceled":
		return true
	}
	return false
}

// readFrames calls each frame of the stream until fn returns false or the
// stream ends. The SSE field rule: the value after the first colon minus one
// optional leading space; comment lines are skipped.
func readFrames(body io.Reader, fn func(frame) bool) error {
	return readFramesRetry(body, nil, fn)
}

// readFramesRetry is readFrames that also keeps the stream's advertised
// reconnect interval (an SSE `retry:` line, milliseconds) in retry when set.
func readFramesRetry(body io.Reader, retry *time.Duration, fn func(frame) bool) error {
	r := bufio.NewReader(body)
	cur := frame{}
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			return err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			if cur.Event != "" {
				if !fn(cur) {
					return nil
				}
			}
			cur = frame{}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		k, v, _ := strings.Cut(line, ":")
		v = strings.TrimPrefix(v, " ")
		switch k {
		case "retry":
			if ms, err := strconv.Atoi(v); err == nil && ms >= 0 && retry != nil {
				*retry = time.Duration(ms) * time.Millisecond
			}
		case "id":
			cur.ID = v
		case "event":
			cur.Event = v
		case "data":
			if err := json.Unmarshal([]byte(v), &cur.Data); err != nil {
				return fmt.Errorf("frame data: %w", err)
			}
		}
	}
}

// openStream opens API-04 for the operation after lastEventID ("" from the start).
func (c *client) openStream(ctx context.Context, operationID, lastEventID string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/api/v1/operations/"+operationID+"/events", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Accept", "text/event-stream")
	if lastEventID != "" {
		req.Header.Set("Last-Event-ID", lastEventID)
	}
	resp, err := c.stream.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, &statusError{code: resp.StatusCode}
	}
	return resp, nil
}
