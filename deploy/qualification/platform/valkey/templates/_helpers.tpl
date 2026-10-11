{{/*
Expand the name of the chart.
*/}}
{{- define "valkey.name" -}}
{{- include "cloudpirates.name" . -}}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "valkey.fullname" -}}
{{- include "cloudpirates.fullname" . -}}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "valkey.chart" -}}
{{- include "cloudpirates.chart" . -}}
{{- end }}

{{/*
Common labels
*/}}
{{- define "valkey.labels" -}}
{{- include "cloudpirates.labels" . -}}
{{- end }}

{{/*
Common annotations
*/}}
{{- define "valkey.annotations" -}}
{{- include "cloudpirates.annotations" . -}}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "valkey.selectorLabels" -}}
{{- include "cloudpirates.selectorLabels" . -}}
{{- end }}

{{/*
Return the proper Valkey image name
*/}}
{{- define "valkey.image" -}}
{{- include "cloudpirates.image" (dict "image" .Values.image "global" .Values.global) -}}
{{- end }}

{{/*
Return Valkey credentials secret name
*/}}
{{- define "valkey.secretName" -}}
{{- if .Values.auth.existingSecret -}}
    {{- include "cloudpirates.tplvalues.render" (dict "value" .Values.auth.existingSecret "context" .) -}}
{{- else -}}
    {{- include "valkey.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
Return Valkey password key
*/}}
{{- define "valkey.passwordKey" -}}
{{- if .Values.auth.existingSecret -}}
    {{- .Values.auth.existingSecretPasswordKey -}}
{{- else -}}
password
{{- end -}}
{{- end }}

{{/*
Return Valkey configuration ConfigMap name
*/}}
{{- define "valkey.configmapName" -}}
{{- if .Values.config.existingConfigmap -}}
    {{- .Values.config.existingConfigmap -}}
{{- else -}}
    {{- include "valkey.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "valkey.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "valkey.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return Valkey data directory
*/}}
{{- define "valkey.dataDir" -}}
{{- printf "/data" -}}
{{- end }}

{{/*
Return Valkey config directory
*/}}
{{- define "valkey.configDir" -}}
{{- printf "/etc/valkey" -}}
{{- end }}

{{/*
Return the proper Docker Image Registry Secret Names
*/}}
{{- define "valkey.imagePullSecrets" -}}
{{ include "cloudpirates.images.renderPullSecrets" (dict "images" (list .Values.image) "context" .) }}
{{- end -}}

{{/*
Return the proper Valkey Sentinel image name
*/}}
{{- define "valkey.sentinel.image" -}}
{{- include "cloudpirates.image" (dict "image" .Values.sentinel.image "global" .Values.global) -}}
{{- end }}

{{/*
Return Valkey config full path name
*/}}
{{- define "valkey.configFullName" -}}
{{- printf "%s/valkey.conf" (include "valkey.configDir" .) -}}
{{- end }}

{{/*
Return the proper master-proxy (HAProxy) image name
*/}}
{{- define "valkey.masterProxy.image" -}}
{{- include "cloudpirates.image" (dict "image" .Values.sentinel.masterProxy.image "global" .Values.global) -}}
{{- end }}

{{/*
Return the proper Valkey exporter image name
*/}}
{{- define "valkey.metrics.image" -}}
{{- include "cloudpirates.image" (dict "image" .Values.metrics.image "global" .Values.global) -}}
{{- end }}

{{/*
AnvilKit patch 3 (P0.6): valkey-cli's options for a Sentinel, which with tls.enabled
listens on TLS only (tls-port, port 0) and reaches the servers and the other
Sentinels over TLS (tls-replication), verified against the mounted CA.
*/}}
{{- define "valkey.sentinelTlsArgs" -}}
{{- if .Values.tls.enabled }} --tls --cert /etc/valkey/tls/{{ .Values.tls.certFilename }} --key /etc/valkey/tls/{{ .Values.tls.certKeyFilename }} --cacert /etc/valkey/tls/{{ .Values.tls.certCAFilename }}{{- end }}
{{- end -}}
