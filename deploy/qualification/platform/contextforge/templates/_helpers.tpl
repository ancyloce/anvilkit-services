{{- define "anvilkit-contextforge.labels" -}}
app.kubernetes.io/name: contextforge
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: anvilkit
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
{{- end -}}

{{- define "anvilkit-contextforge.selectorLabels" -}}
app.kubernetes.io/name: contextforge
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "anvilkit-contextforge.require" -}}
{{- if and (not .Values.ssrfProtection) (not .Values.development.enabled) }}
{{- fail "ssrfProtection false is DEVELOPMENT_ONLY: it renders only with development.enabled: true" }}
{{- end }}
{{- if not .Values.image.digest }}
{{- fail "image.digest is required: the reviewed ContextForge image is pinned by digest" }}
{{- end }}
{{- range $name, $s := dict "database" .Values.database.secret "jwt" .Values.jwt.secret "encryption" .Values.encryption.secret "admin.password" .Values.admin.password.secret }}
{{- if not $s.name }}{{ fail (printf "%s.secret.name is required: an existing Secret (nothing is created by this chart)" $name) }}{{ end }}
{{- end }}
{{- if not .Values.tls.issuerRef.name }}
{{- fail "tls.issuerRef.name is required: the platform issuer of the serving certificate MCP verifies" }}
{{- end }}
{{- if lt (int .Values.replicaCount) 2 }}
{{- fail "replicaCount must be at least 2 (lock.yaml contextforge: two replicas)" }}
{{- end }}
{{- end -}}
