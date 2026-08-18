import { useState } from "react";
import type { FormEvent } from "react";
import { Loader2, UploadCloud } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useCreateSyntheticScan, useUploadScan } from "@/api/patients";
import { ApiError } from "@/api/client";
import { toast } from "@/store/toast";

interface RunScanDialogProps {
  patientId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function RunScanDialog({ patientId, open, onOpenChange }: RunScanDialogProps) {
  const createSynthetic = useCreateSyntheticScan(patientId);
  const upload = useUploadScan(patientId);

  const [method, setMethod] = useState<"synthetic" | "upload">("synthetic");
  const [severity, setSeverity] = useState(0.5);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submitting = createSynthetic.isPending || upload.isPending;

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (method === "synthetic") {
        await createSynthetic.mutateAsync({ severity });
      } else {
        if (!file) {
          setError("Choose an MRI file (.nii or .nii.gz) to upload.");
          return;
        }
        await upload.mutateAsync(file);
      }
      toast({ variant: "success", title: "Scan processed" });
      onOpenChange(false);
      setFile(null);
    } catch (err) {
      // UnprocessableScanError (422) from a failed skull-strip, empty
      // segmentation, etc. surfaces here with the backend's own message —
      // shown inline rather than as a generic failure.
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Run a new scan</DialogTitle>
          <DialogDescription>
            Runs the real preprocessing → volumetry → fuzzy inference pipeline for this patient.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <Tabs value={method} onValueChange={(v) => setMethod(v as "synthetic" | "upload")}>
            <TabsList className="w-full">
              <TabsTrigger value="synthetic" className="flex-1">
                Synthetic demo scan
              </TabsTrigger>
              <TabsTrigger value="upload" className="flex-1">
                Upload MRI
              </TabsTrigger>
            </TabsList>

            <TabsContent value="synthetic" className="space-y-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <Label htmlFor="severity">Severity</Label>
                <span className="tabular-nums">{severity.toFixed(2)}</span>
              </div>
              <input
                id="severity"
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={severity}
                onChange={(e) => setSeverity(Number(e.target.value))}
                className="w-full accent-primary"
              />
            </TabsContent>

            <TabsContent value="upload" className="space-y-2">
              <label
                htmlFor="rescan-file"
                className="flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed border-border bg-secondary/30 p-6 text-center transition-colors hover:bg-secondary/50"
              >
                <UploadCloud className="size-5 text-muted-foreground" />
                <span className="text-xs text-muted-foreground">
                  {file ? file.name : "Click to choose a .nii or .nii.gz file"}
                </span>
              </label>
              <input
                id="rescan-file"
                type="file"
                accept=".nii,.gz,.nii.gz"
                className="sr-only"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </TabsContent>
          </Tabs>

          {error && (
            <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="submit" disabled={submitting} className="gap-2">
              {submitting && <Loader2 className="size-4 animate-spin" />}
              Run scan
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
