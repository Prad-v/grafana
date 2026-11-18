package chunked

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/grafana/grafana-plugin-sdk-go/backend"
	"github.com/stretchr/testify/require"
)

func TestAccumulator(t *testing.T) {
	rsp, err := AccumulateJSONLines(strings.NewReader(`
	 {"refId":"A","frameId":"f0","frame":{"schema":{"refId":"A","fields":[{"name":"f1","type":"number","typeInfo":{"frame":"int64","nullable":true}},{"name":"f2","type":"string","typeInfo":{"frame":"string","nullable":true}},{"name":"f3","type":"boolean","typeInfo":{"frame":"bool","nullable":true}}]},"data":{"values":[[1],["two"],[false]]}}}
	`))
	require.NoError(t, err)

	out, err := json.MarshalIndent(rsp, "", "  ")
	// fmt.Printf("%s\n", out)
	require.NoError(t, err)
	require.JSONEq(t, `{
		"results": {
			"A": {
				"status": 200,
				"frames": [
					{
						"schema": {
							"refId": "A",
							"fields": [
								{
									"name": "f1",
									"type": "number",
									"typeInfo": {
										"frame": "int64",
										"nullable": true
									}
								},
								{
									"name": "f2",
									"type": "string",
									"typeInfo": {
										"frame": "string",
										"nullable": true
									}
								},
								{
									"name": "f3",
									"type": "boolean",
									"typeInfo": {
										"frame": "bool",
										"nullable": true
									}
								}
							]
						},
						"data": {
							"values": [
								[ 1 ], [ "two" ], [ false ]
							]
						}
					}
				]
			}
		}
	}`, string(out))
}

func TestAccumulatorErrorStatus(t *testing.T) {
	rsp, err := AccumulateJSONLines(strings.NewReader(`
	 {"refId":"A","status":401,"error":"unauthorized","errorSource":"downstream"}
	`))
	require.NoError(t, err)
	require.Equal(t, backend.StatusUnauthorized, rsp.Responses["A"].Status)
	require.EqualError(t, rsp.Responses["A"].Error, "unauthorized")
	require.Equal(t, backend.ErrorSourceDownstream, rsp.Responses["A"].ErrorSource)

	_, err = AccumulateJSONLines(strings.NewReader(`{"refId":"A","status":"bad"}`))
	require.ErrorContains(t, err, "expected number")
}
