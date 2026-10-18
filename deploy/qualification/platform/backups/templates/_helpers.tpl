{{- define "anvilkit-backups.check" -}}
{{- if not (and .Values.images.curl .Values.images.mc) }}{{ fail "images.curl and images.mc are required (by digest)" }}{{ end }}
{{- if not (and (contains "@sha256:" .Values.images.curl) (contains "@sha256:" .Values.images.mc)) }}{{ fail "images are pinned by digest" }}{{ end }}
{{- if not .Values.dr.url }}{{ fail "dr.url names the DR object store" }}{{ end }}
{{- if and (not .Values.development) (not (hasPrefix "https://" .Values.dr.url)) }}{{ fail "dr.url must be https outside development" }}{{ end }}
{{- end -}}

{{- define "anvilkit-backups.labels" -}}
app.kubernetes.io/name: anvilkit-backups
app.kubernetes.io/part-of: anvilkit-backups
{{- end -}}

{{/* One store's CronJob: the fetch init container (curl) writes the run's
credentials and snapshot files under /work, the upload container (mc) sends
them to the DR store. */}}
{{- define "anvilkit-backups.cronjob" -}}
{{- $ := .root }}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: anvilkit-backup-{{ .store }}
  labels:
    {{- include "anvilkit-backups.labels" $ | nindent 4 }}
    anvilkit.io/backup: {{ .store }}
spec:
  schedule: {{ index $.Values.schedules .store | quote }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: {{ $.Values.job.successfulJobsHistoryLimit }}
  failedJobsHistoryLimit: {{ $.Values.job.failedJobsHistoryLimit }}
  jobTemplate:
    metadata:
      labels:
        {{- include "anvilkit-backups.labels" $ | nindent 8 }}
        anvilkit.io/backup: {{ .store }}
    spec:
      activeDeadlineSeconds: {{ $.Values.job.activeDeadlineSeconds }}
      backoffLimit: {{ $.Values.job.backoffLimit }}
      template:
        metadata:
          labels:
            {{- include "anvilkit-backups.labels" $ | nindent 12 }}
            anvilkit.io/backup: {{ .store }}
        spec:
          restartPolicy: Never
          serviceAccountName: anvilkit-backups
          automountServiceAccountToken: false
          securityContext: {runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532, seccompProfile: {type: RuntimeDefault}}
          initContainers:
            - name: fetch
              image: {{ $.Values.images.curl }}
              command: ["sh", "/scripts/fetch.sh", {{ .store | quote }}]
              env:
                - {name: OPENBAO_ADDR, value: {{ $.Values.openbao.address | quote }}}
                - {name: OPENBAO_ROLE, value: {{ $.Values.openbao.role | quote }}}
                - {name: OPENBAO_KV, value: {{ $.Values.openbao.kvPath | quote }}}
                - {name: QDRANT_URL, value: {{ $.Values.qdrant.url | quote }}}
              resources: {requests: {cpu: 10m, memory: 32Mi}, limits: {cpu: 500m, memory: 256Mi}}
              securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
              volumeMounts:
                - {name: scripts, mountPath: /scripts, readOnly: true}
                - {name: work, mountPath: /work}
                - {name: token, mountPath: /var/run/secrets/openbao, readOnly: true}
                - {name: trust, mountPath: /etc/anvilkit/trust, readOnly: true}
          containers:
            - name: upload
              image: {{ $.Values.images.mc }}
              command: ["sh", "/scripts/upload.sh", {{ .store | quote }}]
              env:
                - {name: HOME, value: /work}
                - {name: MC_CONFIG_DIR, value: /work/.mc}
                - {name: DR_URL, value: {{ $.Values.dr.url | quote }}}
                - {name: DR_SNAPSHOTS, value: {{ $.Values.dr.snapshotsBucket | quote }}}
                - {name: DR_OBJECTS, value: {{ $.Values.dr.objectsBucket | quote }}}
                - {name: OBJECTS_URL, value: {{ $.Values.objects.url | quote }}}
                - {name: OBJECTS_BUCKETS, value: {{ join " " $.Values.objects.buckets | quote }}}
              resources: {requests: {cpu: 10m, memory: 64Mi}, limits: {cpu: 1, memory: 512Mi}}
              securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
              volumeMounts:
                - {name: scripts, mountPath: /scripts, readOnly: true}
                - {name: work, mountPath: /work}
                - {name: trust, mountPath: /etc/anvilkit/trust, readOnly: true}
          volumes:
            - name: scripts
              configMap: {name: anvilkit-backups-scripts}
            # Memory-backed: the run's credentials and snapshots never reach a node disk.
            - name: work
              emptyDir: {medium: Memory, sizeLimit: 2Gi}
            - name: token
              projected:
                sources:
                  - serviceAccountToken: {audience: {{ $.Values.openbao.audience | quote }}, expirationSeconds: 600, path: token}
            - name: trust
              configMap: {name: {{ $.Values.trust.configMap }}, items: [{key: ca.crt, path: ca.crt}]}
{{- end -}}
