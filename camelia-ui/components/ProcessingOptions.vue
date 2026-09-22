<template>
    <div class="container">
        <div class="mb-4">
            <h2 class="text-xl text-foam mb-2">Processing Options</h2>
            <p class="text-sm text-text">Choose how Camelia should process your images</p>
        </div>

        <div class="bg-surface rounded-lg p-6 border border-overlay">
            <div class="space-y-6" v-auto-animate>
                <!-- Processing Type Option -->
                <div class="option-group">
                    <div class="mb-3 flex items-center justify-between gap-3">
                        <div>
                            <label class="block text-foam text-sm">Processing sequence</label>
                            <p class="mt-1 text-xs text-subtle">Selected passes run in the order shown.</p>
                        </div>
                        <button
                            type="button"
                            class="text-xs text-iris hover:text-foam"
                            @click="selectAll">
                            Select all
                        </button>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3" v-auto-animate>
                        <!-- Ordered multi-select option cards -->
                        <label
                            v-for="option in processingOptions"
                            :key="option.value"
                            class="relative cursor-pointer rounded-md border border-overlay p-4 hover:border-iris transition-colors"
                            :class="{
                                'border-iris bg-highlight-low': localProcessingTypes.includes(option.value)
                            }">
                            <input
                                type="checkbox"
                                :value="option.value"
                                v-model="localProcessingTypes"
                                class="absolute h-0 w-0 opacity-0" />
                            <div class="flex items-center">
                                <div class="flex-shrink-0">
                                    <div
                                        class="h-5 w-5 rounded-full border flex items-center justify-center border-subtle"
                                        :class="{
                                            'border-iris': localProcessingTypes.includes(option.value)
                                        }">
                                        <div
                                            v-if="localProcessingTypes.includes(option.value)"
                                            class="h-3 w-3 rounded-sm bg-iris"></div>
                                    </div>
                                </div>
                                <div class="ml-3">
                                    <span
                                        class="block text-sm"
                                        :class="
                                            localProcessingTypes.includes(option.value)
                                                ? 'text-foam'
                                                : 'text-text'
                                        ">
                                        {{ option.label }}
                                    </span>
                                    <span class="mt-1 block text-xs text-subtle">
                                        {{ option.description }}
                                    </span>
                                </div>
                            </div>
                        </label>
                    </div>
                </div>

                <label class="flex items-center gap-2 text-sm text-text">
                    <input type="checkbox" :checked="reprocess" @change="emit('update:reprocess', ($event.target as HTMLInputElement).checked)" />
                    Reprocess selected methods even if already recorded in ComicInfo.xml
                </label>

                <!-- Process/Cancel Button -->
                <div class="pt-4 flex justify-end">
                    <button
                        @click="handleButtonClick"
                        :disabled="!canProcess && !isProcessing"
                        class="px-5 py-2.5 rounded-md text-base font-medium transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-base"
                        :class="[
                            isProcessing
                                ? 'bg-love hover:bg-rose text-base shadow-sm'
                                : canProcess
                                ? 'bg-iris hover:bg-foam text-base shadow-sm'
                                : 'bg-muted text-subtle cursor-not-allowed'
                        ]">
                        <div class="flex items-center space-x-2">
                            <Icon v-if="isProcessing" name="lucide:x" class="w-4 h-4" />
                            <Icon v-else name="lucide:play" />
                            <span>{{ isProcessing ? 'Cancel' : 'Process Selection' }}</span>
                        </div>
                    </button>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';

type ProcessingType = 'black_bars' | 'white_bars' | 'transparent_black' | 'mosaic';

interface ProcessingOption {
    value: ProcessingType;
    label: string;
    description: string;
}

const processingOptions: ProcessingOption[] = [
    {
        value: 'black_bars',
        label: 'Black Bars',
        description: 'Decensor black censoring bars'
    },
    {
        value: 'transparent_black',
        label: 'Transparent Black',
        description: 'Decensor semi-transparent censoring bars'
    },
    {
        value: 'white_bars',
        label: 'White Bars',
        description: 'Decensor white censoring bars'
    },
    {
        value: 'mosaic',
        label: 'Mosaic',
        description: 'Detect and reconstruct mosaic censorship with Aletheia-Lens'
    }
];

const props = defineProps<{
    processingTypes: ProcessingType[];
    canProcess: boolean;
    isProcessing: boolean;
    reprocess: boolean;
}>();

const emit = defineEmits<{
    (e: 'update:processingTypes', types: ProcessingType[]): void;
    (e: 'update:reprocess', value: boolean): void;
    (e: 'process'): void;
    (e: 'cancel'): void;
}>();

const localProcessingTypes = ref<ProcessingType[]>([...props.processingTypes]);

watch(
    localProcessingTypes,
    (newValue) => {
        const selected = new Set(newValue);
        emit(
            'update:processingTypes',
            processingOptions
                .map((option) => option.value)
                .filter((value) => selected.has(value))
        );
    },
    { deep: true }
);

watch(
    () => props.processingTypes,
    (newValue) => {
        localProcessingTypes.value = [...newValue];
    },
    { deep: true }
);

function selectAll(): void {
    localProcessingTypes.value = processingOptions.map((option) => option.value);
}

function handleButtonClick() {
    if (props.isProcessing) {
        emit('cancel');
    } else {
        emit('process');
    }
}
</script>
