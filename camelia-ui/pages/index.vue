<template>
    <main v-auto-animate>
        <FileUploader
            v-model:selectedFiles="selectedFiles"
            v-model:localCbzPath="localCbzPath"
            v-model:outputLocation="outputLocation"
            v-model:customOutputRoot="customOutputRoot" />

        <ProcessingOptions
            v-model:processingTypes="processingTypes"
            v-model:reprocess="reprocess"
            :canProcess="canProcess"
            :isProcessing="isProcessing"
            @process="processImages"
            @cancel="cancelProcessing" />

        <LibraryBacklog />

        <LoadingConsole
            :message="processingMessage"
            :logs="processLogs"
            :isProcessing="isProcessing"
            :isVisible="isProcessing || showLogs"
            @closeConsole="hideConsole" />

        <ResultsDisplay :results="results" :archive-results="archiveResults" />

        <ErrorMessage :message="errorMessage" />
    </main>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue';
import FileUploader from '@/components/FileUploader.vue';
import ProcessingOptions from '@/components/ProcessingOptions.vue';
import LibraryBacklog from '@/components/LibraryBacklog.vue';
import LoadingConsole from '~/components/LoadingConsole.vue';
import ResultsDisplay from '@/components/ResultsDisplay.vue';
import ErrorMessage from '@/components/ErrorMessage.vue';
import {
    cancelProcessingJob,
    checkProcessingStatus,
    getArchiveDownloadUrl,
    listLocalCbzSources,
    processImages as apiProcessImages,
    streamProcessingLogs,
    transformResults
} from '@/services/api';
import type {
    ArchiveResult,
    OutputLocation,
    ProcessingStatus,
    ProcessingType,
    ResultItem
} from '@/services/api';

type DisplayArchiveResult = ArchiveResult & { downloadUrl: string | null };

interface ProcessingJob {
    label: string;
    files: File[];
    localPath: string;
    relativePath: string;
}

const selectedFiles = ref<File[]>([]);
const localCbzPath = ref('');
const outputLocation = ref<OutputLocation>('automatic');
const customOutputRoot = ref('');
const processingTypes = ref<ProcessingType[]>(['black_bars']);
const reprocess = ref(false);
const isProcessing = ref(false);
const showLogs = ref(false);
const results = ref<ResultItem[]>([]);
const archiveResults = ref<DisplayArchiveResult[]>([]);
const errorMessage = ref('');
const processLogs = ref<string[]>([]);
const currentSessionId = ref<string | null>(null);
const currentJobNumber = ref(0);
const totalJobs = ref(0);
const currentJobLabel = ref('');
let logStreamCleanup: (() => void) | null = null;
let originalObjectUrls: string[] = [];
let cancelRequested = false;

const processingMessage = computed(() => {
    if (totalJobs.value > 1) {
        return `Processing book ${currentJobNumber.value} of ${totalJobs.value}: ${currentJobLabel.value}`;
    }
    return currentJobLabel.value
        ? `Processing ${currentJobLabel.value}`
        : 'Processing images, this may take some time...';
});

function fileLabel(file: File): string {
    return file.webkitRelativePath || file.name;
}

function isCbzFile(file: File): boolean {
    return /\.cbz$/i.test(file.name);
}

function clearOriginalObjectUrls(): void {
    originalObjectUrls.forEach((url) => URL.revokeObjectURL(url));
    originalObjectUrls = [];
}

const canProcess = computed(() => {
    const hasUploads = selectedFiles.value.length > 0;
    const hasLocalPath = localCbzPath.value.trim().length > 0;
    if (hasUploads === hasLocalPath) return false;

    const hasArchives = selectedFiles.value.some(isCbzFile);
    const hasImages = selectedFiles.value.some((file) => !isCbzFile(file));
    const validOutput = outputLocation.value !== 'custom' || customOutputRoot.value.trim().length > 0;
    return !(hasArchives && hasImages) && processingTypes.value.length > 0 && validOutput;
});

function hasComixDirectory(path: string): boolean {
    const parent = path.replace(/\\/g, '/').split('/').slice(0, -1);
    return parent.some((part) => part.toLowerCase() === 'comix');
}

function uploadedArchiveIsEligible(relativePath: string): boolean {
    if (outputLocation.value === 'custom' && hasComixDirectory(`${customOutputRoot.value}/file.cbz`)) {
        return true;
    }
    return hasComixDirectory(relativePath);
}

async function createJobQueue(): Promise<ProcessingJob[]> {
    const localPath = localCbzPath.value.trim();
    if (localPath) {
        const sourcePaths = localPath
            .split(/\r?\n/)
            .map((path) => path.trim())
            .filter(Boolean);
        const { sources, skipped } = await listLocalCbzSources(
            sourcePaths,
            outputLocation.value,
            customOutputRoot.value
        );
        skipped.forEach((archive) => {
            processLogs.value.push(
                `[FILTER] Skipped ${archive.path}: ${archive.reason} (${archive.destination})`
            );
        });
        if (sources.length === 0) {
            throw new Error('No eligible CBZ files: every destination was outside a comix directory.');
        }
        return sources.map((archive) => ({
            label: archive.name,
            files: [],
            localPath: archive.path,
            relativePath: archive.relative_path
        }));
    }

    const archiveFiles = selectedFiles.value.filter(isCbzFile);
    if (archiveFiles.length > 0) {
        const sortedFiles = [...archiveFiles]
            .sort((left, right) =>
                fileLabel(left).localeCompare(fileLabel(right), undefined, {
                    numeric: true,
                    sensitivity: 'base'
                })
            );
        const eligible = sortedFiles.filter((file) => {
            const accepted = uploadedArchiveIsEligible(fileLabel(file));
            if (!accepted) {
                processLogs.value.push(
                    `[FILTER] Skipped ${fileLabel(file)}: destination is not under a comix directory.`
                );
            }
            return accepted;
        });
        if (eligible.length === 0) {
            throw new Error('No eligible CBZ files: every destination was outside a comix directory.');
        }
        return eligible.map((file) => ({
                label: fileLabel(file),
                files: [file],
                localPath: '',
                relativePath: fileLabel(file)
            }));
    }

    return [{
        label: `${selectedFiles.value.length} image${selectedFiles.value.length === 1 ? '' : 's'}`,
        files: selectedFiles.value,
        localPath: '',
        relativePath: ''
    }];
}

function delay(milliseconds: number): Promise<void> {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForJob(sessionId: string): Promise<ProcessingStatus> {
    currentSessionId.value = sessionId;
    logStreamCleanup = streamProcessingLogs(sessionId, (log) => {
        if (log.trim()) processLogs.value.push(log);
    });

    try {
        while (true) {
            const status = await checkProcessingStatus(sessionId);
            if (status.status !== 'processing') return status;
            await delay(1500);
        }
    } finally {
        logStreamCleanup?.();
        logStreamCleanup = null;
        currentSessionId.value = null;
    }
}

function recordCompletedJob(
    status: ProcessingStatus,
    sessionId: string,
    job: ProcessingJob
): void {
    if (status.results) {
        results.value = transformResults(sessionId, status.results, job.files);
        originalObjectUrls = results.value
            .map((result) => result.original)
            .filter((url) => url.startsWith('blob:'));
    }

    if (status.archive) {
        archiveResults.value.push({
            ...status.archive,
            downloadUrl: status.archive.downloadable
                ? getArchiveDownloadUrl(sessionId, status.archive.filename)
                : null
        });
    }
}

async function processImages(): Promise<void> {
    if (!canProcess.value || isProcessing.value) return;

    isProcessing.value = true;
    showLogs.value = true;
    errorMessage.value = '';
    cancelRequested = false;
    clearOriginalObjectUrls();
    results.value = [];
    archiveResults.value = [];
    processLogs.value = [];

    const failedJobs: string[] = [];

    try {
        const jobs = await createJobQueue();
        totalJobs.value = jobs.length;

        for (let index = 0; index < jobs.length; index++) {
            if (cancelRequested) break;

            const job = jobs[index];
            currentJobNumber.value = index + 1;
            currentJobLabel.value = job.label;
            processLogs.value.push(
                `--- Book ${currentJobNumber.value} of ${totalJobs.value}: ${job.label} ---`
            );

            try {
                const { sessionId } = await apiProcessImages(
                    job.files,
                    processingTypes.value,
                    job.localPath,
                    job.relativePath,
                    outputLocation.value,
                    customOutputRoot.value,
                    reprocess.value
                );
                const status = await waitForJob(sessionId);

                if (status.status === 'completed') {
                    recordCompletedJob(status, sessionId, job);
                } else if (status.status === 'cancelled') {
                    cancelRequested = true;
                } else {
                    failedJobs.push(job.label);
                    processLogs.value.push(`Batch warning: ${job.label} failed; continuing.`);
                }
            } catch (error) {
                const message = error instanceof Error ? error.message : 'Unknown processing error';
                failedJobs.push(job.label);
                processLogs.value.push(`Batch warning: ${job.label} failed: ${message}`);
            }
        }

        if (cancelRequested) {
            processLogs.value.push('Batch cancelled. Remaining books were not started.');
        } else {
            const completedCount = jobs.length - failedJobs.length;
            processLogs.value.push(
                `Batch finished: ${completedCount} completed, ${failedJobs.length} failed.`
            );
        }

        if (failedJobs.length > 0) {
            errorMessage.value = `${failedJobs.length} archive(s) failed. Their originals were kept. Check the logs for details.`;
        }

        if (/\.cbz$/i.test(localCbzPath.value.trim()) && archiveResults.value.length === 1) {
            localCbzPath.value = '';
        }
    } catch (error) {
        console.error('Error preparing processing jobs:', error);
        errorMessage.value = error instanceof Error
            ? error.message
            : 'An error occurred while preparing the processing batch';
    } finally {
        finishProcessing();
    }
}

function finishProcessing(): void {
    isProcessing.value = false;
    logStreamCleanup?.();
    logStreamCleanup = null;
    currentSessionId.value = null;
    currentJobNumber.value = 0;
    totalJobs.value = 0;
    currentJobLabel.value = '';
    showLogs.value = true;
}

function hideConsole(): void {
    showLogs.value = false;
}

async function cancelProcessing(): Promise<void> {
    cancelRequested = true;
    processLogs.value.push('Stopping after the current book...');
    if (currentSessionId.value) {
        await cancelProcessingJob(currentSessionId.value);
    }
}

onUnmounted(() => {
    cancelRequested = true;
    clearOriginalObjectUrls();
    logStreamCleanup?.();
});
</script>
