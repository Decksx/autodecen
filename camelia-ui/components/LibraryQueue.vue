<template>
    <div class="rounded-md bg-base border border-overlay p-4 space-y-4">
        <div class="flex items-center justify-between gap-3">
            <h3 class="text-sm text-foam">Book queue</h3>
            <button class="text-sm text-iris underline" :aria-expanded="expanded" @click="expanded = !expanded">
                {{ expanded ? 'Hide queue' : 'View and edit queue' }}
            </button>
        </div>
        <template v-if="expanded">
            <p class="text-xs text-subtle">
                Process next overrides the usual short-book order and respects library schedules.
                Hold removes a book from processing until you return it to the queue. Archives stay on disk.
            </p>
            <form class="flex flex-col sm:flex-row gap-2" @submit.prevent="search">
                <label class="flex-1">
                    <span class="block text-xs text-subtle mb-1">Search titles or paths</span>
                    <input v-model="searchText" type="search" maxlength="500" placeholder="Find a book or series"
                        class="w-full rounded border border-overlay bg-surface px-3 py-2 text-sm text-text" />
                </label>
                <label>
                    <span class="block text-xs text-subtle mb-1">Status</span>
                    <select v-model="state" class="rounded border border-overlay bg-surface px-3 py-2 text-sm text-text">
                        <option v-for="choice in states" :key="choice.value" :value="choice.value">
                            {{ choice.label }}{{ page ? ` (${choice.value === 'all' ? Object.values(page.counts).reduce((a, b) => a + b, 0) : page.counts[choice.value] ?? 0})` : '' }}
                        </option>
                    </select>
                </label>
                <label>
                    <span class="block text-xs text-subtle mb-1">Library</span>
                    <select v-model="root" class="rounded border border-overlay bg-surface px-3 py-2 text-sm text-text">
                        <option value="">All libraries</option>
                        <option v-for="item in roots" :key="item" :value="item">{{ item }}</option>
                    </select>
                </label>
                <button class="self-end rounded px-3 py-2 text-sm bg-highlight-med text-text disabled:opacity-40" :disabled="loading">Search</button>
            </form>

            <div class="flex flex-wrap gap-2 items-center">
                <span class="text-xs text-subtle">{{ selected.length }} selected</span>
                <button class="queue-button" :disabled="busy || !selectedQueued" @click="act('next')">Process selected next</button>
                <button class="queue-button" :disabled="busy || !selectedQueued" @click="act('normal')">Use normal order</button>
                <button class="queue-button" :disabled="busy || !selectedQueued" @click="act('hold')">Hold selected</button>
                <button class="queue-button" :disabled="busy || !selectedHeld" @click="act('release')">Return selected to queue</button>
                <button class="queue-button ml-auto" :disabled="loading || busy" @click="load()">Refresh</button>
            </div>
            <p v-if="error" role="alert" class="text-sm text-love">{{ error }}</p>
            <p v-if="notice" role="status" class="text-sm text-foam">{{ notice }}</p>

            <div class="max-h-[32rem] overflow-auto rounded border border-overlay" :aria-busy="loading">
                <table class="w-full text-left text-xs text-text">
                    <thead class="sticky top-0 bg-surface text-subtle">
                        <tr>
                            <th class="p-3"><input type="checkbox" aria-label="Select editable books on this page" :checked="allSelected" :disabled="busy || !editableRows.length" @change="togglePage" /></th>
                            <th class="p-3">Book</th><th class="p-3">Status / order</th><th class="p-3">Processing</th><th class="p-3">Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="book in page?.items ?? []" :key="book.id" class="border-t border-overlay align-top">
                            <td class="p-3"><input v-model="selected" type="checkbox" :value="book.id" :aria-label="`Select ${title(book)}`" :disabled="busy || !['queued', 'held'].includes(book.state)" /></td>
                            <td class="p-3 min-w-64 max-w-xl">
                                <div class="text-sm text-foam">{{ title(book) }}</div>
                                <div class="mt-1 break-all text-subtle">{{ book.source_path }}</div>
                                <div v-if="book.archive_page_count !== null" class="mt-1 text-subtle">{{ book.archive_page_count < 0 ? 'Page count unavailable' : `${book.archive_page_count} pages` }}</div>
                                <div v-if="book.last_error" class="mt-1 break-all text-love">{{ book.last_error }}</div>
                            </td>
                            <td class="p-3 min-w-28">
                                <div>{{ book.state === 'processing' ? 'Processing · locked' : book.state }}</div>
                                <div v-if="book.state === 'queued'" class="mt-1 text-subtle">{{ book.queue_priority > 0 ? 'Process next' : book.deferred ? 'After shorter books' : 'Normal order' }}</div>
                                <div v-if="book.current_stage" class="mt-1 text-gold">{{ book.current_stage }}</div>
                            </td>
                            <td class="p-3 min-w-32">
                                <div>{{ book.auto_detect ? 'Automatic' : 'Selected methods' }}</div>
                                <div class="mt-1 text-subtle">{{ book.selected_sequence.map(methodLabel).join(' → ') }}</div>
                            </td>
                            <td class="p-3 min-w-44">
                                <div class="flex flex-wrap gap-2">
                                    <button class="queue-button" :disabled="busy" @click="emit('inspect', book.source_path)">View progress</button>
                                    <button v-if="['queued', 'held', 'failed', 'skipped'].includes(book.state)" class="queue-button" :disabled="busy" @click="edit(book)">
                                        {{ book.state === 'failed' || book.state === 'skipped' ? 'Retry / edit' : 'Edit methods' }}
                                    </button>
                                </div>
                                <p v-if="book.state === 'quarantined'" class="mt-2 text-subtle">Restore the archive to its library before retrying.</p>
                            </td>
                        </tr>
                        <tr v-if="page && !page.items.length"><td colspan="5" class="p-6 text-center text-subtle">No books match these filters.</td></tr>
                        <tr v-if="!page && loading"><td colspan="5" class="p-6 text-center text-subtle">Loading books…</td></tr>
                    </tbody>
                </table>
            </div>
            <div class="flex items-center justify-between gap-3 text-xs text-subtle">
                <span>{{ page?.total ? `${offset + 1}–${offset + page.items.length} of ${page.total} books` : '0 books' }}</span>
                <div class="flex gap-2">
                    <button class="queue-button" :disabled="loading || busy || offset === 0" @click="navigate(-50)">Previous</button>
                    <button class="queue-button" :disabled="loading || busy || !page || offset + 50 >= page.total" @click="navigate(50)">Next</button>
                </div>
            </div>

            <form v-if="editing" class="rounded border border-iris bg-surface p-4 space-y-3" @submit.prevent="save">
                <h4 class="text-sm text-foam">Edit {{ title(editing) }}</h4>
                <label class="flex gap-2 items-center text-sm text-text"><input v-model="automatic" type="checkbox" />Use automatic processing</label>
                <p v-if="automatic" class="text-xs text-subtle">Uses the backlog's automatic settings and skips methods already recorded in this archive.</p>
                <template v-else>
                    <div class="flex flex-wrap gap-4 text-sm text-text">
                        <label v-for="method in methods" :key="method.value" class="flex gap-2 items-center"><input v-model="selectedMethods" type="checkbox" :value="method.value" />{{ method.label }}</label>
                    </div>
                    <label class="flex gap-2 items-center text-xs text-text"><input v-model="reprocess" type="checkbox" />Repeat selected methods even if already applied</label>
                    <p class="text-xs text-subtle">Selected methods run across the entire book, overriding automatic page detection.</p>
                </template>
                <p v-if="editing.state === 'held'" class="text-xs text-subtle">Saving returns this held book to the queue.</p>
                <div class="flex gap-2">
                    <button class="queue-button bg-iris text-base" :disabled="busy || (!automatic && !selectedMethods.length)">Save and queue book</button>
                    <button type="button" class="queue-button" :disabled="busy" @click="editing = null">Cancel edit</button>
                </div>
            </form>
        </template>
    </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { editLibraryQueueBooks, getLibraryQueueBooks, requeueLibraryBook } from '@/services/api';
import type { LibraryQueueBook, LibraryQueueEdit, LibraryQueuePage, ProcessingType } from '@/services/api';

defineProps<{ roots: string[] }>();
const emit = defineEmits<{ changed: []; inspect: [path: string] }>();
const expanded = ref(true);
const page = ref<LibraryQueuePage | null>(null);
const state = ref('queued');
const root = ref('');
const searchText = ref('');
const query = ref('');
const offset = ref(0);
const selected = ref<number[]>([]);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const editing = ref<LibraryQueueBook | null>(null);
const automatic = ref(true);
const selectedMethods = ref<ProcessingType[]>([]);
const reprocess = ref(false);
const states = [
    { value: 'queued', label: 'Queued' }, { value: 'held', label: 'Held' },
    { value: 'processing', label: 'Processing' }, { value: 'failed', label: 'Failed' },
    { value: 'quarantined', label: 'Issues' }, { value: 'skipped', label: 'Skipped' },
    { value: 'completed', label: 'Completed' }, { value: 'all', label: 'All books' }
];
const methods: Array<{ value: ProcessingType; label: string }> = [
    { value: 'black_bars', label: 'Black bars' }, { value: 'transparent_black', label: 'Transparent black' },
    { value: 'white_bars', label: 'White bars' }, { value: 'mosaic', label: 'Mosaic' }
];
const editableRows = computed(() => page.value?.items.filter(b => ['queued', 'held'].includes(b.state)) ?? []);
const chosen = computed(() => page.value?.items.filter(b => selected.value.includes(b.id)) ?? []);
const selectedQueued = computed(() => chosen.value.length > 0 && chosen.value.every(b => b.state === 'queued'));
const selectedHeld = computed(() => chosen.value.length > 0 && chosen.value.every(b => b.state === 'held'));
const allSelected = computed(() => editableRows.value.length > 0 && editableRows.value.every(b => selected.value.includes(b.id)));
let requestVersion = 0;
let timer: ReturnType<typeof setInterval> | null = null;
function title(book: LibraryQueueBook): string { return book.metadata_title || book.source_path.split(/[\\/]/).pop() || book.source_path; }
function methodLabel(method: ProcessingType): string { return methods.find(m => m.value === method)?.label ?? method; }
function togglePage(): void { selected.value = allSelected.value ? [] : editableRows.value.map(b => b.id); }
async function load(): Promise<void> {
    const version = ++requestVersion;
    loading.value = true;
    try {
        const result = await getLibraryQueueBooks(state.value, query.value, root.value, offset.value);
        if (version !== requestVersion) return;
        if (offset.value > 0 && offset.value >= result.total) {
            offset.value = Math.max(0, Math.floor((result.total - 1) / 50) * 50);
            await load(); return;
        }
        page.value = result;
        selected.value = selected.value.filter(id => result.items.some(b => b.id === id && ['queued', 'held'].includes(b.state)));
        error.value = '';
    } catch (cause) {
        if (version === requestVersion) error.value = cause instanceof Error ? cause.message : 'Could not load the queue';
    } finally { if (version === requestVersion) loading.value = false; }
}
function search(): void { query.value = searchText.value.trim(); offset.value = 0; selected.value = []; void load(); }
function navigate(delta: number): void { offset.value += delta; selected.value = []; void load(); }
async function run(action: () => Promise<void>): Promise<void> {
    busy.value = true; error.value = ''; notice.value = '';
    try { await action(); selected.value = []; emit('changed'); await load(); }
    catch (cause) { error.value = cause instanceof Error ? cause.message : 'Queue edit failed'; }
    finally { busy.value = false; }
}
function act(action: LibraryQueueEdit): void {
    const ids = chosen.value.map(b => b.id);
    void run(async () => {
        await editLibraryQueueBooks(ids, action);
        notice.value = `${ids.length} book${ids.length === 1 ? '' : 's'} updated.`;
    });
}
function edit(book: LibraryQueueBook): void {
    editing.value = book; automatic.value = Boolean(book.auto_detect);
    selectedMethods.value = [...book.selected_sequence]; reprocess.value = false;
    notice.value = ''; error.value = '';
}
function save(): void {
    if (!editing.value) return;
    const book = editing.value;
    const sequence = methods.map(m => m.value).filter(m => selectedMethods.value.includes(m));
    void run(async () => {
        await requeueLibraryBook(book.source_path, sequence, automatic.value ? false : reprocess.value, automatic.value);
        editing.value = null; notice.value = 'Processing settings saved. Book queued.';
    });
}
watch([state, root], () => { offset.value = 0; selected.value = []; void load(); });
watch(expanded, value => { if (value) void load(); });
onMounted(() => {
    void load();
    timer = setInterval(() => { if (expanded.value && !loading.value && !busy.value && !editing.value) void load(); }, 5000);
});
onUnmounted(() => { ++requestVersion; if (timer) clearInterval(timer); });
</script>

<style scoped>
.queue-button {
    padding: 0.4rem 0.65rem;
    border: 1px solid var(--color-overlay, #403d52);
    border-radius: 0.25rem;
    font-size: 0.75rem;
    cursor: pointer;
}
.queue-button:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
