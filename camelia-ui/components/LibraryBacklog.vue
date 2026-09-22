<template>
    <section class="container mt-8">
        <div class="mb-4">
            <h2 class="text-xl text-foam mb-2">Library Decensor Backlog</h2>
            <p class="text-sm text-text">
                Crawl a library incrementally and queue only methods not yet recorded in ComicInfo.xml.
                Legacy books with unknown method history stay skipped unless manually overridden.
            </p>
        </div>

        <div class="bg-surface rounded-lg p-6 border border-overlay space-y-5">
            <div class="flex flex-col lg:flex-row gap-3 lg:items-end">
                <label class="flex-1">
                    <span class="block text-sm text-foam mb-2">Library root</span>
                    <select v-model="selectedRoot" class="w-full rounded-md bg-base border border-overlay px-3 py-2 text-text">
                        <option v-for="root in roots" :key="root" :value="root">{{ root }}</option>
                    </select>
                </label>
                <div class="flex flex-wrap gap-2">
                    <button class="rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 bg-iris text-base" :disabled="busy || scan?.status === 'running'" @click="startScan">
                        Start / resume crawl
                    </button>
                    <button class="rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 bg-gold text-base" :disabled="busy || !scan || scan.status !== 'running'" @click="scanAction('pause')">
                        Pause crawl
                    </button>
                    <button class="rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 bg-love text-base" :disabled="busy || !scan || !['running', 'paused'].includes(scan.status)" @click="scanAction('stop')">
                        Stop crawl
                    </button>
                </div>
            </div>

            <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
                <div v-for="item in totals" :key="item.label" class="rounded-md bg-highlight-low p-3 border border-overlay">
                    <div class="text-xs text-subtle">{{ item.label }}</div>
                    <div class="text-xl text-foam">{{ item.value }}</div>
                </div>
            </div>

            <div class="rounded-md bg-base border border-overlay p-4 space-y-2 text-sm">
                <div class="flex flex-wrap gap-x-6 gap-y-1">
                    <span class="text-text">Crawl: <strong class="text-foam">{{ scan?.status ?? 'not started' }}</strong></span>
                    <span class="text-text">Queue: <strong class="text-foam">{{ status?.queue_status ?? 'paused' }}</strong></span>
                    <span v-if="status?.current_stage" class="text-text">Stage: <strong class="text-foam">{{ status.current_stage }}</strong></span>
                </div>
                <div class="text-xs text-subtle break-all">
                    {{ status?.current_file || 'No current file' }}
                </div>
            </div>

            <div class="flex flex-wrap gap-2">
                <button class="rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 bg-iris text-base" :disabled="busy || !status?.backup_offload_configured || (selectedRoot.toLowerCase() === 'x:\\comix' && (!status?.comic_automation_handoff_configured || !status?.source_registration_configured)) || status?.queue_status === 'running'" @click="queueAction('resume')">
                    Resume queue
                </button>
                <button class="rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 bg-gold text-base" :disabled="busy || status?.queue_status !== 'running'" @click="queueAction('pause')">
                    Pause after current book
                </button>
                <button class="rounded-md px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 bg-love text-base" :disabled="busy || status?.queue_status === 'stopped'" @click="queueAction('stop')">
                    Stop after current book
                </button>
            </div>
            <p v-if="actionError" class="text-sm text-love">{{ actionError }}</p>

            <div class="rounded-md bg-base border border-overlay p-4 space-y-2 text-sm text-text">
                <label class="flex items-center gap-2">
                    <input type="checkbox" :checked="status?.batch_schedule?.enabled ?? false"
                        :disabled="busy || !status?.comic_automation_handoff_configured || !status?.source_registration_configured || !status?.backup_offload_configured"
                        @change="scheduleChanged" />
                    Run X:\comix batches only in the scheduled windows
                </label>
                <p class="text-xs text-subtle">
                    Every night 12am–5am and weekdays 9am–5pm, America/Denver.
                    A book already running finishes before the queue waits. Camelia must stay running; you can close this tab.
                </p>
                <p class="text-xs text-love">
                    {{ status?.comic_automation_handoff_configured
                        ? 'Handoff configured. Verify its dry run before a large batch.'
                        : 'ComicAutomation handoff is not configured, so X: processing is blocked.' }}
                    {{ status?.source_registration_configured
                        ? 'Untracked X: books are registered and hashed before model processing.'
                        : 'X: source registration is not configured.' }}
                    {{ status?.backup_offload_configured
                        ? 'Originals are verified on C: for replacement, then moved into series folders on F:. The queue pauses if offload fails.'
                        : 'F: original-backup offload is not configured, so backlog processing is blocked.' }}
                </p>
                <p v-if="status?.batch_schedule?.enabled" class="text-xs text-foam">
                    {{ status.queue_status !== 'running' ? 'Scheduled queue paused — press Resume queue' :
                        (status.batch_schedule.in_window ? 'Window open' : 'Waiting for next window') }}
                    <span v-if="status.batch_schedule.next_window_at"> · Next: {{ formatWindowTime(status.batch_schedule.next_window_at) }}</span>
                    <span v-if="status.batch_schedule.window_ends_at"> · Ends: {{ formatWindowTime(status.batch_schedule.window_ends_at) }}</span>
                </p>
            </div>

            <div class="rounded-md bg-base border border-overlay p-4 space-y-3">
                <h3 class="text-sm text-foam">Queue one previously crawled book</h3>
                <input v-model="bookPath" type="text" placeholder="Full path to a CBZ in the crawled library"
                    class="w-full rounded-md bg-surface border border-overlay px-3 py-2 text-sm text-text" />
                <div class="flex flex-wrap gap-3 text-xs text-text">
                    <label v-for="method in methodChoices" :key="method.value" class="flex items-center gap-1">
                        <input v-model="manualMethods" type="checkbox" :value="method.value" />
                        {{ method.label }}
                    </label>
                </div>
                <label class="flex items-center gap-2 text-xs text-text">
                    <input v-model="manualReprocess" type="checkbox" />
                    Reprocess selected methods even if their tags are already present
                </label>
                <button class="rounded-md px-4 py-2 text-sm font-medium bg-iris text-base disabled:opacity-40"
                    :disabled="busy || !bookPath.trim() || manualMethods.length === 0" @click="queueBook">
                    Queue this book
                </button>
                <button class="rounded-md px-4 py-2 text-sm font-medium bg-highlight-med text-text disabled:opacity-40"
                    :disabled="busy || !bookPath.trim()" @click="lookupBook">
                    Show pass progress
                </button>
            </div>

            <div class="rounded-md bg-base border border-overlay p-4 space-y-3">
                <h3 class="text-sm text-foam">Archive pass progress</h3>
                <p class="text-xs text-subtle">A finished pass is only marked applied after the rebuilt CBZ is installed and its method tags are verified. Showing the current and recent jobs; enter a path above to look up another book.</p>
                <p v-if="!visibleBooks.length" class="text-xs text-subtle">No processed archive progress yet.</p>
                <div v-for="book in visibleBooks" :key="book.id" class="rounded-md border border-overlay bg-surface p-3 space-y-2">
                    <div class="flex flex-wrap justify-between gap-2 text-xs">
                        <span class="text-text break-all">{{ book.source_path }}</span>
                        <span class="text-foam">
                            {{ book.state }} · attempt {{ book.attempts }}
                            <template v-if="book.duration_seconds !== null"> · total {{ formatDuration(book.duration_seconds) }}</template>
                        </span>
                    </div>
                    <div class="grid grid-cols-2 lg:grid-cols-4 gap-2">
                        <div v-for="stage in book.stages" :key="stage.stage" class="rounded border border-overlay px-2 py-1 text-xs"
                            :class="{ 'text-foam border-foam': stage.state === 'completed', 'text-love border-love': stage.state === 'failed', 'text-gold border-gold': stage.state === 'running' || stage.state === 'passed' }">
                            <div>{{ stageLabel(stage.stage) }}</div>
                            <div>{{ stageStatus(stage) }}</div>
                            <div v-if="stage.started_at" class="mt-1 text-[11px] text-subtle">
                                Started {{ formatLocalTimestamp(stage.started_at) }}
                            </div>
                            <div v-if="stage.passed_at" class="text-[11px] text-subtle">
                                Finished {{ formatLocalTimestamp(stage.passed_at) }}
                            </div>
                        </div>
                    </div>
                    <div v-if="book.finalization.length" class="text-xs text-subtle">
                        <div class="mb-1 text-text">Finalization</div>
                        <div v-for="phase in book.finalization" :key="phase.phase">
                            {{ finalizationLabel(phase.phase) }}: {{ phase.state }}
                            <template v-if="phase.duration_seconds !== null"> · {{ formatDuration(phase.duration_seconds) }}</template>
                        </div>
                    </div>
                    <p v-if="book.last_error" class="text-xs text-love break-all">{{ book.last_error }}</p>
                </div>
            </div>

            <p v-if="refreshError" class="text-sm text-love">{{ refreshError }}</p>
            <div v-if="status?.events.length" class="max-h-48 overflow-auto rounded-md bg-base border border-overlay p-3 font-mono text-xs text-subtle">
                <div v-for="event in status.events.slice(-12)" :key="event.id" class="py-1">
                    [{{ formatLocalTimestamp(event.created_at) }}] [{{ event.event_type }}] {{ event.message }}
                </div>
            </div>
        </div>
    </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import {
    controlLibraryQueue,
    controlLibrarySchedule,
    controlLibraryScan,
    getLibraryBacklog,
    getLibraryBookProgress,
    requeueLibraryBook,
    startLibraryScan
} from '@/services/api';
import type { LibraryBacklogStatus, LibraryBookProgress, ProcessingType } from '@/services/api';

const fallbackRoots = ['X:\\comix', 'Z:\\comics\\Comix'];
const status = ref<LibraryBacklogStatus | null>(null);
const selectedRoot = ref(fallbackRoots[0]);
const busy = ref(false);
const actionError = ref('');
const refreshError = ref('');
const bookPath = ref('');
const manualMethods = ref<ProcessingType[]>(['black_bars']);
const manualReprocess = ref(false);
const lookedUpBook = ref<LibraryBookProgress | null>(null);
const methodChoices: Array<{ value: ProcessingType; label: string }> = [
    { value: 'black_bars', label: 'Black bars' },
    { value: 'transparent_black', label: 'Transparent black' },
    { value: 'white_bars', label: 'White bars' },
    { value: 'mosaic', label: 'Mosaic' }
];
let pollTimer: number | null = null;
let selectionInitialized = false;

const roots = computed(() => status.value?.allowed_roots?.length ? status.value.allowed_roots : fallbackRoots);
const scan = computed(() => status.value?.scan ?? null);
const visibleBooks = computed<LibraryBookProgress[]>(() => {
    const recent = status.value?.recent_books ?? [];
    return lookedUpBook.value && !recent.some((book) => book.id === lookedUpBook.value!.id)
        ? [lookedUpBook.value, ...recent] : recent;
});
const totals = computed(() => [
    { label: 'Found', value: scan.value?.total_found ?? 0 },
    { label: 'Scanned', value: scan.value?.scanned_count ?? 0 },
    { label: 'Eligible', value: scan.value?.eligible_count ?? 0 },
    { label: 'Queued', value: status.value?.counts.queued ?? 0 },
    { label: 'Completed', value: status.value?.counts.completed ?? 0 },
    { label: 'Failed', value: status.value?.counts.failed ?? 0 },
    { label: 'Skipped', value: status.value?.counts.skipped ?? 0 }
]);

async function refresh(): Promise<void> {
    try {
        status.value = await getLibraryBacklog();
        if (!selectionInitialized && status.value.scan?.root_path) {
            selectedRoot.value = status.value.scan.root_path;
        }
        selectionInitialized = true;
        refreshError.value = '';
    } catch (cause) {
        refreshError.value = cause instanceof Error ? cause.message : 'Could not load backlog status';
    }
}

async function run(action: () => Promise<unknown>): Promise<void> {
    busy.value = true;
    actionError.value = '';
    try {
        await action();
        await refresh();
    } catch (cause) {
        actionError.value = cause instanceof Error ? cause.message : 'Backlog action failed';
    } finally {
        busy.value = false;
    }
}

function startScan(): void {
    void run(() => startLibraryScan(selectedRoot.value));
}

function scanAction(action: 'pause' | 'stop'): void {
    if (!scan.value) return;
    void run(() => controlLibraryScan(scan.value!.id, action));
}

function queueAction(action: 'pause' | 'resume' | 'stop'): void {
    void run(() => controlLibraryQueue(action));
}

function scheduleChanged(event: Event): void {
    void run(() => controlLibrarySchedule((event.target as HTMLInputElement).checked));
}

function formatWindowTime(value: string): string {
    return new Intl.DateTimeFormat(undefined, {
        weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
        timeZone: 'America/Denver', timeZoneName: 'short'
    }).format(new Date(value));
}

function queueBook(): void {
    const selected = methodChoices.map((item) => item.value).filter((value) => manualMethods.value.includes(value));
    void run(async () => {
        const path = bookPath.value.trim();
        await requeueLibraryBook(path, selected, manualReprocess.value);
        lookedUpBook.value = await getLibraryBookProgress(path);
    });
}

function lookupBook(): void {
    void run(async () => {
        lookedUpBook.value = await getLibraryBookProgress(bookPath.value.trim());
    });
}

function stageLabel(stage: ProcessingType): string {
    return methodChoices.find((item) => item.value === stage)?.label ?? stage;
}

function stageStatus(stage: LibraryBookProgress['stages'][number]): string {
    const duration = stage.duration_seconds !== null ? ` · ${formatDuration(stage.duration_seconds)}` : '';
    if (stage.state === 'completed') return `✓ Applied to archive${duration}`;
    if (stage.state === 'not_selected' && stage.recorded_tag) return '✓ Recorded in ComicInfo';
    const prior = stage.applied_at ? '✓ Applied previously · ' :
        (stage.recorded_tag ? '✓ Previously tagged · ' : '');
    const labels: Record<LibraryBookProgress['stages'][number]['state'], string> = {
        not_selected: 'Not selected', pending: 'Pending', running: 'Running',
        passed: 'Pass finished; not installed', failed: 'Failed',
        interrupted: 'Interrupted', completed: 'Applied to archive'
    };
    return prior + labels[stage.state] + (['passed', 'failed'].includes(stage.state) ? duration : '');
}

function formatDuration(seconds: number): string {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    const parts: string[] = [];
    if (hours) parts.push(`${hours}h`);
    if (minutes) parts.push(`${minutes}m`);
    if (remaining || !parts.length) parts.push(`${remaining}s`);
    return parts.join(' ');
}

function formatLocalTimestamp(value: string): string {
    const date = new Date(value);
    const pad = (part: number) => String(part).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
        + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function finalizationLabel(phase: string): string {
    const labels: Record<string, string> = {
        cbz_rebuild: 'CBZ rebuild',
        rebuilt_archive_verification: 'Rebuilt archive verification',
        comicinfo_tagging: 'ComicInfo.xml tagging',
        original_backup: 'Original archive backup',
        destination_install: 'Destination install/replace',
        installed_archive_verification: 'Installed archive verification'
    };
    return labels[phase] ?? phase.replaceAll('_', ' ');
}

watch(bookPath, () => { lookedUpBook.value = null; });

onMounted(async () => {
    await refresh();
    pollTimer = window.setInterval(refresh, 2000);
});

onUnmounted(() => {
    if (pollTimer !== null) window.clearInterval(pollTimer);
});
</script>
