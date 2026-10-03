"use client";

import { useId, useState } from "react";
import { FileText, Files, FlaskConical, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { loadDemo, uploadJob, uploadResumes } from "@/lib/api";
import type { DemoStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

const ACCEPT = ".pdf,.docx";

function DropZone({
  label,
  hint,
  multiple,
  files,
  onFiles,
  Icon,
}: {
  label: string;
  hint: string;
  multiple: boolean;
  files: File[];
  onFiles: (files: File[]) => void;
  Icon: typeof FileText;
}) {
  const id = useId();
  const [over, setOver] = useState(false);

  function accept(list: FileList | null) {
    if (!list) return;
    const picked = Array.from(list).filter((f) => /\.(pdf|docx)$/i.test(f.name));
    onFiles(multiple ? picked : picked.slice(0, 1));
  }

  return (
    <div>
      <input
        id={id}
        type="file"
        accept={ACCEPT}
        multiple={multiple}
        className="peer sr-only"
        onChange={(e) => accept(e.target.files)}
      />
      <label
        htmlFor={id}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          accept(e.dataTransfer.files);
        }}
        className={cn(
          "flex cursor-pointer items-start gap-3 rounded-xl border border-dashed border-white/15 bg-black/20 p-4 transition-colors hover:border-white/30 peer-focus-visible:ring-2 peer-focus-visible:ring-signal",
          over && "border-signal bg-signal/5"
        )}
      >
        <Icon className="mt-0.5 h-5 w-5 shrink-0 text-signal" aria-hidden />
        <span className="min-w-0 text-sm">
          <span className="block font-medium text-white">{label}</span>
          <span className="block text-mute">
            {files.length === 0
              ? hint
              : files.length === 1
                ? files[0].name
                : `${files.length} files selected`}
          </span>
        </span>
      </label>
    </div>
  );
}

export function UploadPanel({
  disabled,
  demo,
  onUploaded,
}: {
  disabled?: boolean;
  demo?: DemoStatus | null;
  onUploaded: () => void | Promise<void>;
}) {
  const [jd, setJd] = useState<File[]>([]);
  const [resumes, setResumes] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [demoBusy, setDemoBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0);

  async function submit() {
    if (jd.length === 0 || resumes.length === 0) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await uploadJob(jd[0]);
      await uploadResumes(resumes);
      setNote(`Ingested ${resumes.length} resume${resumes.length === 1 ? "" : "s"}.`);
      setJd([]);
      setResumes([]);
      setResetKey((k) => k + 1);
      await onUploaded();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  }

  async function useSampleData() {
    setDemoBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = await loadDemo();
      setNote(
        result.added > 0
          ? `Loaded a sample job description and ${result.added} sample candidates. Next: click "Screen and email candidates".`
          : "Sample data is already loaded."
      );
      await onUploaded();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load sample data.");
    } finally {
      setDemoBusy(false);
    }
  }

  const working = busy || demoBusy;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Upload documents</CardTitle>
        <CardDescription>PDF or DOCX. One job description, any number of resumes.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <DropZone
          key={`jd-${resetKey}`}
          label="Job description"
          hint="Choose or drop a file"
          multiple={false}
          files={jd}
          onFiles={setJd}
          Icon={FileText}
        />
        <DropZone
          key={`cv-${resetKey}`}
          label="Resumes"
          hint="Choose or drop one or more files"
          multiple
          files={resumes}
          onFiles={setResumes}
          Icon={Files}
        />
        <Button
          className="w-full"
          onClick={submit}
          disabled={disabled || working || jd.length === 0 || resumes.length === 0}
        >
          {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {busy ? "Uploading" : "Upload and ingest"}
        </Button>

        {demo?.available && (
          <div className="space-y-2 border-t border-white/10 pt-3">
            <p className="text-sm text-mute">
              No files? Load a sample job description and {demo.candidates} sample candidates to try the full pipeline.
            </p>
            <Button
              variant="outline"
              className="w-full"
              onClick={useSampleData}
              disabled={disabled || working || demo.loaded}
            >
              {demoBusy ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : (
                <FlaskConical className="h-4 w-4" aria-hidden />
              )}
              {demo.loaded ? "Sample data loaded" : "Try with sample data"}
            </Button>
          </div>
        )}

        {note && (
          <p role="status" className="text-sm text-ok">
            {note}
          </p>
        )}
        {error && (
          <p role="alert" className="break-words text-sm text-bad">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
