import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { motion } from "framer-motion";
import {
  AlertCircle,
  ArrowLeft,
  CalendarDays,
  CheckCircle2,
  Plus,
  ScanLine,
  Sparkles,
  UserRound,
} from "lucide-react";
import { usePatient, useReviewScan } from "@/api/patients";
import { useBiomarkerDefs } from "@/api/fis";
import {
  STAGE_BADGE_VARIANT,
  STAGE_LABEL,
  confidenceTone,
  describeIndeterminate,
} from "@/lib/stage-style";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { ConfidenceRing } from "@/components/patient/confidence-ring";
import { MembershipBars } from "@/components/patient/membership-bars";
import { BiomarkerGauge } from "@/components/patient/biomarker-gauge";
import { BiomarkerMembershipChart } from "@/components/patient/membership-chart";
import { RuleList } from "@/components/patient/rule-list";
import { ScanStatusBanner } from "@/components/patient/scan-status-banner";
import { RunScanDialog } from "@/components/patient/run-scan-dialog";
import { toast } from "@/store/toast";
import { PageSkeleton } from "@/components/layout/page-skeleton";

export function PatientDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const patientQuery = usePatient(id);
  const biomarkerDefsQuery = useBiomarkerDefs();
  const reviewMutation = useReviewScan(id ?? "");

  const [selectedScanId, setSelectedScanId] = useState<string | null>(null);
  const [runScanOpen, setRunScanOpen] = useState(false);

  const patient = patientQuery.data;
  const scans = patient?.scans ?? [];
  const scan = scans.find((s) => s.id === selectedScanId) ?? scans[0] ?? null;

  useEffect(() => {
    if (patientQuery.isError) {
      toast({
        variant: "destructive",
        title: "Couldn't load this patient",
        description:
          patientQuery.error instanceof Error ? patientQuery.error.message : undefined,
      });
      navigate("/", { replace: true });
    }
  }, [patientQuery.isError, patientQuery.error, navigate]);

  if (patientQuery.isLoading || biomarkerDefsQuery.isLoading) return <PageSkeleton />;
  if (!patient) return null; // redirecting via the effect above

  const result = scan?.fisResult ?? null;
  const biomarkerDefs = biomarkerDefsQuery.data ?? [];

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="mx-auto max-w-6xl space-y-6"
    >
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        Back to triage dashboard
      </Link>

      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <div className="flex items-center gap-3">
          <div className="flex size-12 items-center justify-center rounded-xl bg-secondary">
            <UserRound className="size-6 text-muted-foreground" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold tracking-tight">{patient.displayId}</h2>
              {result?.stage ? (
                <Badge variant={STAGE_BADGE_VARIANT[result.stage]}>
                  {STAGE_LABEL[result.stage]}
                </Badge>
              ) : result ? (
                <Badge variant="outline">Indeterminate</Badge>
              ) : null}
              {result?.needsReview && (
                <Badge variant="warning" className="gap-1">
                  <Sparkles className="size-3" /> Flagged
                </Badge>
              )}
            </div>
            <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <UserRound className="size-3" /> {patient.age}y · {patient.sex}
              </span>
              <span className="flex items-center gap-1">
                <ScanLine className="size-3" /> {patient.source}
              </span>
              {scan && (
                <span className="flex items-center gap-1">
                  <CalendarDays className="size-3" /> {scan.scanDate}
                </span>
              )}
              {scan?.externalSubjectId && (
                <span className="font-mono">{scan.externalSubjectId}</span>
              )}
              {result?.calibrationVersion && (
                <span>calibration {result.calibrationVersion}</span>
              )}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button variant="outline" className="gap-2" onClick={() => setRunScanOpen(true)}>
            <Plus className="size-4" />
            New scan
          </Button>
          {scan && result && (
            <Button
              variant={result.reviewed ? "secondary" : "default"}
              disabled={reviewMutation.isPending}
              onClick={() =>
                reviewMutation.mutate({ scanId: scan.id, reviewed: !result.reviewed })
              }
              className="gap-2"
            >
              <CheckCircle2 className="size-4" />
              {result.reviewed ? "Reviewed by clinician" : "Mark as reviewed"}
            </Button>
          )}
        </div>
      </div>

      {scans.length > 1 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted-foreground">Scan history:</span>
          {scans.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelectedScanId(s.id)}
              className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                s.id === scan?.id
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:bg-secondary"
              }`}
            >
              {s.scanDate}{" "}
              {s.fisResult ? `· ${s.fisResult.stage ?? "indeterminate"}` : `· ${s.status}`}
            </button>
          ))}
        </div>
      )}

      {!scan ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <p className="text-sm text-muted-foreground">
              No scans yet for this patient. Run one to get a staging result.
            </p>
            <Button className="gap-2" onClick={() => setRunScanOpen(true)}>
              <Plus className="size-4" />
              Run first scan
            </Button>
          </CardContent>
        </Card>
      ) : (
        <>
          {scan.status !== "completed" && <ScanStatusBanner scan={scan} />}

          {result && (
            <>
              <div className="grid gap-4 lg:grid-cols-3">
                <Card className="lg:col-span-1">
                  <CardHeader>
                    <CardTitle>Staging output</CardTitle>
                    <CardDescription>Defuzzified result and confidence</CardDescription>
                  </CardHeader>
                  {result.determinate ? (
                    <CardContent className="flex flex-col items-center gap-5">
                      <ConfidenceRing
                        value={result.confidence}
                        tone={confidenceTone(result.confidence)}
                        label="confidence"
                      />
                      <div className="w-full border-t border-border pt-4">
                        <div className="mb-2 flex items-center justify-between text-xs">
                          <span className="text-muted-foreground">Uncertainty score</span>
                          <span className="font-medium tabular-nums">{result.uncertainty}%</span>
                        </div>
                        {result.severity != null && (
                          <div className="mb-3 flex items-center justify-between text-xs">
                            <span className="text-muted-foreground">
                              Severity index (0 = CN, 1 = AD; provisional)
                            </span>
                            <span className="font-medium tabular-nums">
                              {result.severity.toFixed(2)}
                            </span>
                          </div>
                        )}
                        <MembershipBars membership={result.membership} />
                      </div>
                    </CardContent>
                  ) : (
                    <CardContent className="space-y-3">
                      <div className="flex items-start gap-2.5 rounded-lg border border-status-warning/40 bg-status-warning/10 p-3">
                        <AlertCircle className="mt-0.5 size-4 shrink-0 text-status-warning" />
                        <div>
                          <p className="text-sm font-medium">Not staged</p>
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            The engine declined to give a stage for this scan, so it is flagged
                            for clinician review.
                          </p>
                        </div>
                      </div>
                      <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
                        {describeIndeterminate(result.indeterminateReason).map((line) => (
                          <li key={line}>{line}</li>
                        ))}
                      </ul>
                    </CardContent>
                  )}
                </Card>

                <div className="lg:col-span-2">
                  <Card className="h-full">
                    <CardHeader>
                      <CardTitle>Volumetric biomarkers</CardTitle>
                      <CardDescription>Extracted from automated brain volumetry</CardDescription>
                    </CardHeader>
                    <CardContent className="grid gap-3 sm:grid-cols-2">
                      {biomarkerDefs.map((def) => {
                        const reading = scan.biomarkers.find((b) => b.key === def.key);
                        if (!reading) return null;
                        return <BiomarkerGauge key={def.key} def={def} reading={reading} />;
                      })}
                    </CardContent>
                  </Card>
                </div>
              </div>

              <div className="grid gap-4 md:grid-cols-3">
                {biomarkerDefs.map((def) => {
                  const reading = scan.biomarkers.find((b) => b.key === def.key);
                  if (!reading) return null;
                  return <BiomarkerMembershipChart key={def.key} def={def} value={reading.value} />;
                })}
              </div>

              {result.firedRules.length > 0 && <RuleList rules={result.firedRules} />}
            </>
          )}
        </>
      )}

      <RunScanDialog patientId={patient.id} open={runScanOpen} onOpenChange={setRunScanOpen} />
    </motion.div>
  );
}
