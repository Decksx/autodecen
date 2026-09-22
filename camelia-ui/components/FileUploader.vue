<template>
    <div class="container">
        <div class="mb-4">
            <h2 class="text-xl text-foam mb-2">Select Images, CBZ Archives, or Folders</h2>
            <div class="flex items-center justify-between mb-2">
                <p class="text-sm text-text">Drop a folder to queue all CBZ archives inside it</p>
                <div
                    v-if="selectedFiles.length > 0"
                    class="mt-2 flex justify-end space-x-2"
                    v-auto-animate>
                    <button
                        @click.stop="emit('update:selectedFiles', [])"
                        class="px-2 text-sm text-text hover:text-love transition-colors">
                        Clear all
                    </button>
                </div>
            </div>
        </div>

        <div
            class="drop-zone border-2 border-dashed border-overlay hover:border-iris focus-within:border-iris transition-colors duration-300 rounded-lg p-6 bg-surface cursor-pointer relative"
            :class="{ 'border-foam': isDragging }"
            @dragenter.prevent="handleDragEnter"
            @dragover.prevent="() => {}"
            @dragleave.prevent="handleDragLeave"
            @drop.prevent="handleFileDrop"
            @click="triggerFileInput"
            ref="dropZone">
            <div
                v-if="!selectedFiles.length"
                class="flex flex-col items-center justify-center py-8">
                <div class="mb-4 text-text">
                    <Icon name="lucide:upload-cloud" size="64px" />
                </div>
                <p class="text-center text-text mb-2">
                    <span class="text-foam">Drag & drop images, CBZ files, or a folder</span>
                </p>
                <p class="text-xs text-subtle">Accepted formats: JPEG, PNG, WebP, CBZ</p>
                <button
                    type="button"
                    @click.stop="triggerDirectoryInput"
                    class="mt-4 rounded-md border border-iris px-3 py-2 text-sm text-foam hover:bg-highlight-low">
                    Choose a CBZ folder
                </button>
            </div>

            <div
                v-else
                class="custom-webkit space-y-2 max-h-64 overflow-y-auto pr-2"
                v-auto-animate>
                <div
                    v-for="(file, index) in selectedFiles"
                    :key="index"
                    class="file-item flex items-center bg-highlight-low rounded-md p-3 group hover:bg-highlight-med transition-colors duration-200">
                    <div class="w-10 h-10 mr-3 rounded overflow-hidden bg-overlay">
                        <img
                            v-if="isImageFile(file)"
                            :src="getImagePreview(file)"
                            :alt="file.name"
                            class="w-full h-full object-cover" />
                        <div v-else class="w-full h-full flex items-center justify-center text-text">
                            <Icon name="lucide:book-open" size="24px" />
                        </div>
                    </div>
                    <div class="flex-1 min-w-0">
                        <p class="text-text truncate text-sm">{{ displayFileName(file) }}</p>
                        <p class="text-subtle text-xs">{{ formatFileSize(file.size) }}</p>
                    </div>
                    <button
                        class="remove-btn p-2 text-subtle opacity-0 group-hover:opacity-100 hover:text-love transition-all duration-200"
                        @click.stop="removeFile(index)">
                        <Icon name="lucide:x" size="24px" />
                    </button>
                </div>
            </div>

            <input
                type="file"
                ref="fileInput"
                @change="handleFileSelect"
                multiple
                accept="image/*,.cbz,application/vnd.comicbook+zip"
                class="hidden" />

            <input
                type="file"
                ref="directoryInput"
                @change="handleDirectorySelect"
                multiple
                webkitdirectory
                accept=".cbz,application/vnd.comicbook+zip"
                class="hidden" />

            <div
                v-if="isDragging"
                class="absolute inset-0 bg-base bg-opacity-50 flex items-center justify-center rounded-lg">
                <div class="flex flex-col items-center">
                    <div class="w-16 h-16 text-text">
                        <Icon name="lucide:upload-cloud" size="64px" />
                    </div>
                    <p class="text-foam text-lg">Release to upload</p>
                </div>
            </div>
        </div>

        <details class="mt-3 rounded-lg border border-overlay bg-surface p-4">
            <summary class="cursor-pointer text-sm text-foam">
                Use local paths (best for very long paths or large batches)
            </summary>
            <p class="mt-2 text-xs text-subtle">
                This bypasses browser upload limits. Enter one local CBZ file or folder per line;
                folders are scanned recursively.
            </p>
            <textarea
                id="local-cbz-path"
                :value="localCbzPath"
                @input="updateLocalCbzPath"
                rows="3"
                placeholder="C:\Library\comix&#10;Z:\Incoming\comix"
                class="mt-3 w-full resize-y rounded-md border border-overlay bg-base px-3 py-2 text-text focus:border-iris focus:outline-none"></textarea>
        </details>

        <div class="mt-4 rounded-lg border border-overlay bg-surface p-4">
            <label for="output-root" class="mt-4 block text-sm text-foam mb-2">
                Output directory
            </label>
            <select
                id="output-root"
                :value="outputLocation"
                @change="updateOutputLocation"
                class="w-full rounded-md border border-overlay bg-base px-3 py-2 text-text focus:border-iris focus:outline-none">
                <option value="automatic">Automatic (beside local files; Camelia output for uploads)</option>
                <option value="camelia">Camelia output folder (camelia-decensor\output\archives)</option>
                <option value="custom">Custom folder…</option>
            </select>
            <input
                v-if="outputLocation === 'custom'"
                type="text"
                :value="customOutputRoot"
                @input="updateCustomOutputRoot"
                placeholder="C:\Library\comix or Z:\Processed\comix"
                class="mt-3 w-full rounded-md border border-overlay bg-base px-3 py-2 text-text focus:border-iris focus:outline-none" />
            <p class="mt-2 text-xs text-subtle">
                Folder structure is mirrored and original CBZ filenames are always kept. When an
                output beside the source would overwrite it, Camelia adds “ - AI Decensored”.
            </p>
            <p class="mt-2 text-xs text-gold">
                Only CBZ files whose final destination is inside a folder named “comix” are
                processed. Manga and graphic-novel destinations are skipped and logged.
            </p>
            <p
                v-if="localCbzPath.trim() && selectedFiles.length"
                class="mt-2 text-sm text-love">
                Clear the uploaded files or clear the local path before processing.
            </p>
            <p v-else-if="hasMixedSelection" class="mt-2 text-sm text-love">
                Process loose images and CBZ archives in separate batches.
            </p>
        </div>
    </div>
</template>

<script setup lang="ts">
const emit = defineEmits<{
    (e: 'update:selectedFiles', files: File[]): void;
    (e: 'update:localCbzPath', path: string): void;
    (e: 'update:outputLocation', location: OutputLocation): void;
    (e: 'update:customOutputRoot', path: string): void;
}>();

type OutputLocation = 'automatic' | 'camelia' | 'custom';

const props = defineProps<{
    selectedFiles: File[];
    localCbzPath: string;
    outputLocation: OutputLocation;
    customOutputRoot: string;
}>();

const fileInput = ref<HTMLInputElement | null>(null);
const directoryInput = ref<HTMLInputElement | null>(null);
const dropZone = ref<HTMLElement | null>(null);
const isDragging = ref(false);
const imagePreviews = ref<Record<string, string>>({});
const hasMixedSelection = computed(
    () => props.selectedFiles.some(isCbzFile) && props.selectedFiles.some(isImageFile)
);

let dragCounter = 0;

function triggerFileInput(): void {
    fileInput.value?.click();
}

function triggerDirectoryInput(): void {
    directoryInput.value?.click();
}

function updateLocalCbzPath(event: Event): void {
    const target = event.target as HTMLTextAreaElement;
    emit('update:localCbzPath', target.value);
}

function updateOutputLocation(event: Event): void {
    const target = event.target as HTMLSelectElement;
    emit('update:outputLocation', target.value as OutputLocation);
}

function updateCustomOutputRoot(event: Event): void {
    const target = event.target as HTMLInputElement;
    emit('update:customOutputRoot', target.value);
}

function isImageFile(file: File): boolean {
    return /\.(png|jpe?g|webp|avif)$/i.test(file.name);
}

function isSupportedFile(file: File): boolean {
    return isImageFile(file) || /\.cbz$/i.test(file.name);
}

function isCbzFile(file: File): boolean {
    return /\.cbz$/i.test(file.name);
}

function displayFileName(file: File): string {
    return file.webkitRelativePath || file.name;
}

function appendFiles(files: File[]): void {
    const existingKeys = new Set(
        props.selectedFiles.map(
            (file) => `${displayFileName(file)}:${file.size}:${file.lastModified}`
        )
    );
    const additions = files.filter((file) => {
        const key = `${displayFileName(file)}:${file.size}:${file.lastModified}`;
        if (existingKeys.has(key)) return false;
        existingKeys.add(key);
        return true;
    });

    emit('update:selectedFiles', [...props.selectedFiles, ...additions]);
    generatePreviews(additions);
}

function handleFileSelect(event: Event): void {
    const target = event.target as HTMLInputElement;
    if (target.files) {
        const files = Array.from(target.files).filter(isSupportedFile);
        appendFiles(files);
        target.value = '';
    }
}

function handleDirectorySelect(event: Event): void {
    const target = event.target as HTMLInputElement;
    if (target.files) {
        appendFiles(Array.from(target.files).filter(isCbzFile));
        target.value = '';
    }
}

interface DroppedEntry {
    isFile: boolean;
    isDirectory: boolean;
    name: string;
    fullPath?: string;
}

interface DroppedFileEntry extends DroppedEntry {
    file: (success: (file: File) => void, failure?: (error: unknown) => void) => void;
}

interface DroppedDirectoryReader {
    readEntries: (
        success: (entries: DroppedEntry[]) => void,
        failure?: (error: unknown) => void
    ) => void;
}

interface DroppedDirectoryEntry extends DroppedEntry {
    createReader: () => DroppedDirectoryReader;
}

function readDirectoryEntries(reader: DroppedDirectoryReader): Promise<DroppedEntry[]> {
    return new Promise((resolve, reject) => {
        const entries: DroppedEntry[] = [];

        const readNextBatch = () => {
            reader.readEntries((batch) => {
                if (batch.length === 0) {
                    resolve(entries);
                    return;
                }
                entries.push(...batch);
                readNextBatch();
            }, reject);
        };

        readNextBatch();
    });
}

function readDroppedFile(entry: DroppedFileEntry): Promise<File> {
    return new Promise((resolve, reject) => {
        entry.file((file) => {
            const relativePath = entry.fullPath?.replace(/^\//, '');
            if (relativePath) {
                try {
                    Object.defineProperty(file, 'webkitRelativePath', {
                        value: relativePath
                    });
                } catch {
                    // Some browsers expose non-extensible File objects. The
                    // archive is still usable; only its displayed path is lost.
                }
            }
            resolve(file);
        }, reject);
    });
}

async function collectDroppedFiles(entry: DroppedEntry): Promise<File[]> {
    if (entry.isFile) {
        return [await readDroppedFile(entry as DroppedFileEntry)];
    }
    if (!entry.isDirectory) return [];

    const reader = (entry as DroppedDirectoryEntry).createReader();
    const childEntries = await readDirectoryEntries(reader);
    const childFiles = await Promise.all(childEntries.map(collectDroppedFiles));
    return childFiles.flat();
}

function handleDragEnter(e: DragEvent): void {
    dragCounter++;
    isDragging.value = true;
}

function handleDragLeave(e: DragEvent): void {
    dragCounter--;

    const relatedTarget = e.relatedTarget as Node;
    if (dragCounter === 0 || (!dropZone.value?.contains(relatedTarget) && relatedTarget !== null)) {
        isDragging.value = false;
        dragCounter = 0;
    }
}

async function handleFileDrop(event: DragEvent): Promise<void> {
    isDragging.value = false;
    dragCounter = 0;
    if (!event.dataTransfer) return;

    const entryItems = Array.from(event.dataTransfer.items)
        .map((item) => {
            const extendedItem = item as DataTransferItem & {
                webkitGetAsEntry?: () => DroppedEntry | null;
            };
            return extendedItem.webkitGetAsEntry?.() ?? null;
        })
        .filter((entry): entry is DroppedEntry => entry !== null);

    if (entryItems.some((entry) => entry.isDirectory)) {
        try {
            const nestedFiles = await Promise.all(entryItems.map(collectDroppedFiles));
            appendFiles(nestedFiles.flat().filter(isCbzFile));
        } catch (error) {
            console.error('Could not read the dropped directory:', error);
        }
        return;
    }

    appendFiles(Array.from(event.dataTransfer.files).filter(isSupportedFile));
}

function removeFile(index: number): void {
    const updatedFiles = [...props.selectedFiles];
    updatedFiles.splice(index, 1);
    emit('update:selectedFiles', updatedFiles);
}

function generatePreviews(files: File[]): void {
    files.filter(isImageFile).forEach((file) => {
        const reader = new FileReader();
        reader.onload = (e) => {
            if (e.target?.result) {
                imagePreviews.value[file.name] = e.target.result as string;
            }
        };
        reader.readAsDataURL(file);
    });
}

function getImagePreview(file: File): string {
    return imagePreviews.value[file.name] || '';
}

function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}
</script>
