const API_BASE_URL: string = 'http://localhost:5000/api';

export interface ResultItem {
    filename: string;
    original: string;
    processed: string;
}

export interface ArchiveResult {
    filename: string;
    downloadable: boolean;
    local_path: string | null;
    original_deleted: boolean;
}

export interface LocalCbzSource {
    name: string;
    path: string;
    relative_path: string;
    destination: string;
    reason?: string;
}

export type ProcessingType = 'black_bars' | 'transparent_black' | 'white_bars' | 'mosaic';
export type OutputLocation = 'automatic' | 'camelia' | 'custom';

export interface ProcessingStatus {
    status: 'processing' | 'completed' | 'error' | 'cancelled';
    results?: Array<{ filename: string }>;
    archive?: ArchiveResult;
}

export interface ApiResponse {
    success: boolean;
    session_id: string;
    results: Array<{
        filename: string;
    }>;
    error?: string;
}

/**
 * Process images using the Camelia API
 */
export async function processImages(
    files: File[],
    processingTypes: ProcessingType[],
    localCbzPath = '',
    sourceRelativePath = '',
    outputLocation: OutputLocation = 'automatic',
    outputRoot = '',
    reprocess = false
): Promise<{ sessionId: string }> {
    // Create form data with files
    const formData = new FormData();

    // Append each file to the form data
    files.forEach((file) => {
        formData.append('files', file);
    });

    formData.append('model_types', JSON.stringify(processingTypes));
    // Older API versions can still process the first selected pass.
    if (processingTypes[0]) formData.append('model_type', processingTypes[0]);
    if (localCbzPath.trim()) {
        formData.append('source_path', localCbzPath.trim());
    }
    if (sourceRelativePath.trim()) {
        formData.append('source_relative_path', sourceRelativePath.trim());
    }
    if (outputRoot.trim()) {
        formData.append('output_root', outputRoot.trim());
    }
    formData.append('output_location', outputLocation);
    formData.append('reprocess', String(reprocess));

    // Make API request to process images
    const response = await fetch(`${API_BASE_URL}/process`, {
        method: 'POST',
        body: formData
    });

    const data = (await response.json()) as ApiResponse;

    if (!response.ok) {
        throw new Error(data.error || 'Failed to process images');
    }

    if (!data.success || !data.session_id) {
        throw new Error('Invalid response from server');
    }

    return { sessionId: data.session_id };
}

export interface LibraryScanStatus {
    id: number;
    root_path: string;
    status: 'running' | 'paused' | 'stopped' | 'completed' | 'failed';
    current_path: string | null;
    total_found: number;
    scanned_count: number;
    eligible_count: number;
    skipped_count: number;
    parse_error_count: number;
}

export interface LibraryBacklogStatus {
    allowed_roots: string[];
    comic_automation_handoff_configured: boolean;
    source_registration_configured: boolean;
    backup_offload_configured: boolean;
    scan: LibraryScanStatus | null;
    queue_status: 'running' | 'paused' | 'stopped';
    known_types_only: boolean;
    batch_schedule: {
        enabled: boolean;
        auto_resume: boolean;
        root: string;
        timezone: string;
        windows: string;
        in_window: boolean;
        window_ends_at: string | null;
        next_window_at: string | null;
    };
    counts: Record<'discovered' | 'eligible' | 'queued' | 'processing' | 'completed' | 'failed' | 'skipped', number>;
    current_file: string | null;
    current_stage: string | null;
    recent_books: LibraryBookProgress[];
    events: Array<{
        id: number;
        event_type: string;
        message: string;
        created_at: string;
    }>;
}

export interface LibraryBookProgress {
    id: number;
    source_path: string;
    state: string;
    attempts: number;
    current_stage: string | null;
    last_error: string | null;
    started_at: string | null;
    completed_at: string | null;
    duration_seconds: number | null;
    updated_at: string;
    auto_detect: number;
    detected_methods: ProcessingType[] | null;
    detected_pages: Partial<Record<ProcessingType, string[]>> | null;
    detected_page_counts: Partial<Record<ProcessingType, number>> | null;
    detection_checked_at: string | null;
    detection_duration_seconds: number | null;
    detection_page_count: number | null;
    selected_sequence: ProcessingType[];
    stages: Array<{
        stage: ProcessingType;
        recorded_tag: boolean;
        state: 'not_selected' | 'pending' | 'running' | 'passed' | 'completed' | 'failed' | 'interrupted';
        applied_at: string | null;
        started_at: string | null;
        passed_at: string | null;
        duration_seconds: number | null;
        last_error: string | null;
    }>;
    finalization: Array<{
        phase: string;
        state: 'running' | 'completed' | 'failed' | 'interrupted';
        started_at: string | null;
        completed_at: string | null;
        duration_seconds: number | null;
        last_error: string | null;
    }>;
}

async function backlogRequest(path = '', options?: RequestInit): Promise<any> {
    const response = await fetch(`${API_BASE_URL}/library-backlog${path}`, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Library backlog request failed');
    return data;
}

export async function getLibraryBacklog(): Promise<LibraryBacklogStatus> {
    return backlogRequest() as Promise<LibraryBacklogStatus>;
}

export async function getLibraryBookProgress(sourcePath: string): Promise<LibraryBookProgress> {
    return backlogRequest(`/book-progress?source_path=${encodeURIComponent(sourcePath)}`) as Promise<LibraryBookProgress>;
}

export async function startLibraryScan(root: string): Promise<number> {
    const data = await backlogRequest('/scan/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root })
    });
    return data.scan_id as number;
}

export async function controlLibraryScan(
    scanId: number,
    action: 'pause' | 'resume' | 'stop'
): Promise<void> {
    await backlogRequest(`/scan/${scanId}/${action}`, { method: 'POST' });
}

export async function controlLibraryQueue(action: 'pause' | 'resume' | 'stop'): Promise<void> {
    await backlogRequest(`/queue/${action}`, { method: 'POST' });
}

export async function controlLibraryDetectionMode(enabled: boolean): Promise<void> {
    await backlogRequest('/known-types-only', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
    });
}

export async function controlLibrarySchedule(enabled: boolean): Promise<void> {
    await backlogRequest('/schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled })
    });
}

export async function requeueLibraryBook(
    sourcePath: string,
    modelTypes: ProcessingType[],
    reprocess: boolean
): Promise<ProcessingType[]> {
    const data = await backlogRequest('/queue/book', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_path: sourcePath, model_types: modelTypes, reprocess })
    });
    return data.selected_sequence as ProcessingType[];
}

export async function listLocalCbzSources(
    sourcePaths: string[],
    outputLocation: OutputLocation,
    outputRoot = ''
): Promise<{ sources: LocalCbzSource[]; skipped: LocalCbzSource[] }> {
    const response = await fetch(`${API_BASE_URL}/local-cbz-sources`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            source_paths: sourcePaths.map((path) => path.trim()),
            output_location: outputLocation,
            output_root: outputRoot.trim()
        })
    });

    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.error || 'Failed to read the local CBZ directory');
    }

    return {
        sources: data.sources as LocalCbzSource[],
        skipped: (data.skipped ?? []) as LocalCbzSource[]
    };
}

/**
 * Check the status of a processing job
 */
export async function checkProcessingStatus(sessionId: string): Promise<ProcessingStatus> {
    const response = await fetch(`${API_BASE_URL}/status/${sessionId}`);

    if (!response.ok) {
        throw new Error('Failed to check processing status');
    }

    const data = await response.json();

    return {
        status: data.status,
        results: data.results,
        archive: data.archive
    };
}

export function getArchiveDownloadUrl(sessionId: string, filename: string): string {
    return `${API_BASE_URL}/archives/${sessionId}/${encodeURIComponent(filename)}`;
}

/**
 * Stream logs from the processing job
 */
export function streamProcessingLogs(sessionId: string, onLog: (log: string) => void): () => void {
    const evtSource = new EventSource(`${API_BASE_URL}/logs/${sessionId}`);

    evtSource.onmessage = (event) => {
        if (event.data && event.data.trim()) {
            onLog(event.data);
            setTimeout(() => {
            }, 0);
        }
    };

    evtSource.onerror = () => {
        console.error('EventSource failed:', sessionId);
        evtSource.close();
    };

    return () => {
        evtSource.close();
    };
}

/**
 * Transform API results to the format for display
 */
export function transformResults(
    sessionId: string,
    results: Array<{ filename: string }>,
    originalFiles: File[] = []
): ResultItem[] {
    const imageFiles = originalFiles.filter((file) => /\.(png|jpe?g|webp|avif)$/i.test(file.name));
    const originalFilesByStem = new Map(
        imageFiles.map((file) => [file.name.replace(/\.[^.]+$/, '').toLowerCase(), file])
    );

    return results.map((result, index) => {
        const resultStem = result.filename.replace(/\.[^.]+$/, '').toLowerCase();
        const originalFile = originalFilesByStem.get(resultStem) ?? imageFiles[index];

        return {
            filename: result.filename,
            original: originalFile ? URL.createObjectURL(originalFile) : '',
            processed: `${API_BASE_URL}/results/${sessionId}/${result.filename}`
        };
    });
}

/**
 * Cancel a running processing job
 */
export async function cancelProcessingJob(sessionId: string): Promise<boolean> {
    try {
        const response = await fetch(`${API_BASE_URL}/cancel/${sessionId}`, {
            method: 'POST'
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.error || 'Failed to cancel processing');
        }

        const data = await response.json();
        return data.success === true;
    } catch (error) {
        console.error('Error cancelling job:', error);
        return false;
    }
}
