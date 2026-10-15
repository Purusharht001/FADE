import { useState, useEffect } from "react";
import type { FormEvent } from "react";
import { Loader2, UploadCloud, Sparkles } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useCreateSyntheticScan, useUploadScan, useCreateBiomarkersScan } from "@/api/patients";
import { simulateFIS } from "@/api/fis";
import { ApiError } from "@/api/client";
import { toast } from "@/store/toast";
import type { FISResult } from "@/types/api";

interface RunScanDialogProps {
  patientId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function RunScanDialog({ patientId, open, onOpenChange }: RunScanDialogProps) {
  const createSynthetic = useCreateSyntheticScan(patientId);
  const upload = useUploadScan(patientId);
  const createBiomarkers = useCreateBiomarkersScan(patientId);

  const [method, setMethod] = useState<"biomarkers" | "synthetic" | "upload">("biomarkers");

  // Biomarkers state
  const [hippocampus, setHippocampus] = useState("4.10");
  const [vbr, setVbr] = useState("0.045");
  const [thickness, setThickness] = useState("2.65");
  const [simResult, setSimResult] = useState<FISResult | null>(null);

  const [severity, setSeverity] = useState(0.5);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submitting = createSynthetic.isPending || upload.isPending || createBiomarkers.isPending;

  // Live fuzzy preview effect
  useEffect(() => {
    if (method !== "biomarkers") return;
    const h = Number(hippocampus);
    const v = Number(vbr);
    const t = Number(thickness);
    if (isNaN(h) || isNaN(v) || isNaN(t) || h <= 0 || v < 0 || t <= 0) return;

    let ignore = false;
    simulateFIS({
      hippocampalVolume: h,
      ventricleBrainRatio: v,
      corticalThickness: t,
    })
      .then((res) => {
        if (!ignore) setSimResult(res);
      })
      .catch(() => {});

    return () => {
      ignore = true;
    };
  }, [method, hippocampus, vbr, thickness]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      if (method === "biomarkers") {
        await createBiomarkers.mutateAsync({
          derived: {
            hippocampalVolume: { value: Number(hippocampus), unit: "‰ICV" },
            ventricleBrainRatio: { value: Number(vbr), unit: "ratio" },
            corticalThickness: { value: Number(thickness), unit: "mm" },
          },
        });
      } else if (method === "synthetic") {
        await createSynthetic.mutateAsync({ severity });
      } else {
        if (!file) {
          setError("Choose an MRI file (.nii or .nii.gz) to upload.");
          return;
        }
        await upload.mutateAsync(file);
      }
      toast({ variant: "success", title: "Scan processed and staged" });
      onOpenChange(false);
      setFile(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>Run a new scan</DialogTitle>
          <DialogDescription>
            Evaluate MRI biomarkers through the Fuzzy Inference System.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <Tabs
            value={method}
            onValueChange={(v) => setMethod(v as "biomarkers" | "synthetic" | "upload")}
          >
            <TabsList className="grid w-full grid-cols-3">
              <TabsTrigger value="biomarkers">
                Biomarkers
              </TabsTrigger>
              <TabsTrigger value="synthetic">
                Synthetic Demo
              </TabsTrigger>
              <TabsTrigger value="upload">
                Upload MRI
              </TabsTrigger>
            </TabsList>

            <TabsContent value="biomarkers" className="space-y-3 pt-1">
              <div className="grid grid-cols-3 gap-2.5">
                <div className="flex flex-col gap-1">
                  <Label htmlFor="rescan-hippo" className="text-xs">
                    Hippocampus <span className="text-[10px] text-muted-foreground">(‰ICV)</span>
                  </Label>
                  <Input
                    id="rescan-hippo"
                    type="number"
                    step="0.01"
                    min="0.1"
                    max="15"
                    value={hippocampus}
                    onChange={(e) => setHippocampus(e.target.value)}
                    required
                  />
                  <span className="text-[10px] text-muted-foreground">Normal: 3.6 – 6.4</span>
                </div>
                <div className="flex flex-col gap-1">
                  <Label htmlFor="rescan-vbr" className="text-xs">
                    VBR <span className="text-[10px] text-muted-foreground">(ratio)</span>
                  </Label>
                  <Input
                    id="rescan-vbr"
                    type="number"
                    step="0.001"
                    min="0.001"
                    max="1"
                    value={vbr}
                    onChange={(e) => setVbr(e.target.value)}
                    required
                  />
                  <span className="text-[10px] text-muted-foreground">Normal: &lt; 0.048</span>
                </div>
                <div className="flex flex-col gap-1">
                  <Label htmlFor="rescan-thickness" className="text-xs">
                    Thickness <span className="text-[10px] text-muted-foreground">(mm)</span>
                  </Label>
                  <Input
                    id="rescan-thickness"
                    type="number"
                    step="0.01"
                    min="0.5"
                    max="10"
                    value={thickness}
                    onChange={(e) => setThickness(e.target.value)}
                    required
                  />
                  <span className="text-[10px] text-muted-foreground">Normal: 2.7 – 3.2</span>
                </div>
              </div>

              {simResult && simResult.severity !== null && (
                <div className="rounded-lg border border-primary/25 bg-primary/5 p-3 flex items-center justify-between transition-all">
                  <div className="space-y-1">
                    <div className="flex items-center gap-1.5 text-xs font-semibold text-primary">
                      <Sparkles className="size-3.5" />
                      <span>Fuzzy Auto-Staging</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={
                          simResult.stage === "AD"
                            ? "critical"
                            : simResult.stage === "MCI"
                            ? "warning"
                            : "good"
                        }
                      >
                        {simResult.stage ?? "INDETERMINATE"}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {simResult.confidence.toFixed(1)}% confidence
                      </span>
                    </div>
                  </div>
                  <div className="text-right">
                    <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                      Auto-Calculated Severity
                    </span>
                    <div className="text-lg font-bold tabular-nums text-foreground">
                      {simResult.severity.toFixed(3)}
                    </div>
                  </div>
                </div>
              )}
            </TabsContent>

            <TabsContent value="synthetic" className="space-y-2 pt-1">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <Label htmlFor="severity">Severity Knob</Label>
                <span className="tabular-nums font-medium">{severity.toFixed(2)}</span>
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

            <TabsContent value="upload" className="space-y-2 pt-1">
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
              Run & stage
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

