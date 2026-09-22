<script setup lang="ts">
import { ref } from 'vue';

interface FaqItem {
    question: string;
    answer: string;
    isOpen: boolean;
    category: string;
    icon?: string;
}

const faqItems = ref<FaqItem[]>([
    {
        question: 'What is Camelia?',
        answer: 'Camelia is an image decensor tool for black bars, white bars, transparent black bars, and mosaic censorship. Mosaic restoration uses the separately installed Aletheia-Lens backend.',
        isOpen: false,
        category: 'general',
        icon: 'lucide:info'
    },
    {
        question: 'Wait, this tool seems familiar...',
        answer: 'Yep, it is heavily inspired by Er0mangaDemo by Er0manga, in fact the lama inpainting model is the same.',
        isOpen: false,
        category: 'general',
        icon: 'lucide:info'
    },

    {
        question: 'How accurate is the decensoring process?',
        answer: 'Results are generally pretty good, the accuracy depends on factors such as image quality, censorship type, and the complexity of the censored area.',
        isOpen: false,
        category: 'technical',
        icon: 'lucide:bar-chart'
    },
    {
        question: 'Do I need a powerful GPU for processing?',
        answer: 'While not strictly required, a NVIDIA GPU with CUDA support will significantly speed up the processing. Without a GPU, processing will use CPU, which is much slower particularly for the inpainting step. Weak GPU is sufficient for most images, I am myself using a GTX 1650 and it works fine.',
        isOpen: false,
        category: 'technical',
        icon: 'lucide:cpu'
    },
    {
        question: 'Can I upload images decensored by Camelia to public sites?',
        answer: "No, don't do that. Camelia is intended for personal use only. However I can't stop you, if you do upload them, please DO NOT credit me or Camelia.",
        isOpen: false,
        category: 'general',
        icon: 'lucide:briefcase'
    },
    {
        question: 'What image formats are supported?',
        answer: 'Camelia supports common image formats including PNG, JPEG, and WebP.',
        isOpen: false,
        category: 'usage',
        icon: 'lucide:image'
    },
    {
        question: 'How do I choose between bars and mosaic processing?',
        answer: 'Choose the matching censorship type, or select several stages to run them in the displayed order. Mosaic uses Aletheia-Lens automatic detection, so pages without detected mosaics are left visually unchanged.',
        isOpen: false,
        category: 'usage',
        icon: 'lucide:layers'
    },
    {
        question: 'Can I process multiple images in batch?',
        answer: 'Yes, both the CLI and web interface support batch processing. In the web interface, you can select multiple images at once for upload. In CLI mode, you can place multiple images in the input directory.',
        isOpen: false,
        category: 'usage',
        icon: 'lucide:files'
    },
    {
        question: 'Is my data private when using Camelia?',
        answer: 'Yes, all processing happens locally on your machine. When using the web interface, your images are sent only to your local API server running on your computer. No data is sent to external servers or stored permanently beyond the processing session.',
        isOpen: false,
        category: 'general',
        icon: 'lucide:shield'
    },
    {
        question: 'Will there be a public website later?',
        answer: "Maybe, depends on the demand, however server costs with GPU are expensive, so really don't count on it.",
        isOpen: false,
        category: 'general',
        icon: 'lucide:shield'
    },
    {
        question: "What should I do if the application doesn't start?",
        answer: "First, ensure you've installed all dependencies with 'pip install -r requirements.txt' and, for the UI, 'npm install' in the camelia-ui directory. Check that you're using Python 3.9 and have PyTorch installed correctly. Also verify that you have sufficient disk space and that no other applications are using the required ports (5000 for the API, 3000 for the UI).",
        isOpen: false,
        category: 'troubleshooting',
        icon: 'lucide:power'
    },
    {
        question: 'How do I update Camelia to the latest version?',
        answer: "To update, pull the latest changes from the repository: 'git pull'. Then reinstall dependencies: 'pip install -r requirements.txt' for the backend and 'npm install' in the camelia-ui directory for the frontend. Check the release notes for any specific update instructions.",
        isOpen: false,
        category: 'usage',
        icon: 'lucide:refresh-cw'
    },
    {
        question: 'I have trouble installing x, what should I do?',
        answer: 'Ask in the Issues section of the GitHub repository. Please provide details about your operating system, Python version, and any error messages you encounter.',
        isOpen: false,
        category: 'troubleshooting',
        icon: 'lucide:power'
    }
]);

// Category filters
const categories = [
    { id: 'all', name: 'All Questions', icon: 'lucide:list' },
    { id: 'general', name: 'General', icon: 'lucide:info' },
    { id: 'technical', name: 'Technical', icon: 'lucide:settings' },
    { id: 'usage', name: 'Usage', icon: 'lucide:user' },
    { id: 'troubleshooting', name: 'Troubleshooting', icon: 'lucide:help-circle' }
];

const selectedCategory = ref('all');

const filteredFaqItems = computed(() => {
    if (selectedCategory.value === 'all') {
        return faqItems.value;
    }
    return faqItems.value.filter((item) => item.category === selectedCategory.value);
});

function toggleFaq(index: number) {
    const actualIndex = faqItems.value.findIndex((item) => item === filteredFaqItems.value[index]);
    if (actualIndex !== -1) {
        faqItems.value[actualIndex].isOpen = !faqItems.value[actualIndex].isOpen;
    }
}
</script>

<template>
    <div class="container mx-auto px-4 py-8 max-w-4xl">
        <div class="mb-12 text-center">
            <h1 class="text-4xl font-bold text-foam mb-4">Frequently Asked Questions</h1>
            <p class="text-text text-xl max-w-3xl mx-auto">
                Find answers to common questions about Camelia's features, usage, and
                troubleshooting
            </p>
        </div>

        <!-- Category filters -->
        <div class="mb-8">
            <div class="flex flex-wrap justify-center gap-2">
                <button
                    v-for="category in categories"
                    :key="category.id"
                    @click="selectedCategory = category.id"
                    class="px-4 py-2 rounded-full transition-all flex items-center space-x-2"
                    :class="[
                        selectedCategory === category.id
                            ? 'bg-iris text-base shadow-md'
                            : 'bg-surface hover:bg-highlight-low text-subtle'
                    ]">
                    <Icon :name="category.icon" size="16" />
                    <span>{{ category.name }}</span>
                </button>
            </div>
        </div>

        <!-- FAQ Accordion -->
        <div class="space-y-4">
            <div
                v-for="(item, index) in filteredFaqItems"
                :key="index + item.question"
                class="bg-surface border border-highlight-low rounded-lg overflow-hidden transition-all duration-300"
                :class="{ 'shadow-md': item.isOpen }">
                <!-- Question header -->
                <button
                    @click="toggleFaq(index)"
                    class="w-full p-5 text-left flex items-center justify-between transition-colors duration-300"
                    :class="{ 'bg-highlight-low': item.isOpen }">
                    <div class="flex items-center">
                        <div
                            class="w-8 h-8 rounded-full bg-base flex items-center justify-center mr-3">
                            <Icon
                                :name="item.icon || 'lucide:help-circle'"
                                :class="[
                                    item.category === 'general'
                                        ? 'text-foam'
                                        : item.category === 'technical'
                                        ? 'text-gold'
                                        : item.category === 'usage'
                                        ? 'text-pine'
                                        : 'text-love'
                                ]" />
                        </div>
                        <span class="font-medium text-lg text-text">{{ item.question }}</span>
                    </div>
                    <Icon
                        :name="item.isOpen ? 'lucide:chevron-up' : 'lucide:chevron-down'"
                        class="text-muted transition-transform duration-300"
                        :class="{ 'transform rotate-180': item.isOpen }" />
                </button>

                <!-- Answer content with transition -->
                <Transition
                    enter-active-class="transition-all duration-100 ease-out"
                    leave-active-class="transition-all duration-100 ease-in"
                    enter-from-class="opacity-0 max-h-0"
                    enter-to-class="opacity-100 max-h-96"
                    leave-from-class="opacity-100 max-h-96"
                    leave-to-class="opacity-0 max-h-0">
                    <div v-if="item.isOpen" class="px-5 pb-5 pt-2 overflow-hidden">
                        <div class="pl-11">
                            <p class="text-text">{{ item.answer }}</p>
                        </div>
                    </div>
                </Transition>
            </div>
        </div>

        <!-- No results message -->
        <div v-if="filteredFaqItems.length === 0" class="mt-12 text-center">
            <Icon name="lucide:search-x" size="48" class="text-muted mx-auto mb-4" />
            <h3 class="text-xl font-medium text-foam mb-2">No matching questions found</h3>
            <p class="text-subtle">Try selecting a different category</p>
        </div>

        <!-- Still have questions -->
        <div class="mt-16 p-8 bg-surface rounded-lg border border-highlight-low text-center">
            <h3 class="text-2xl font-bold text-iris mb-4">Still have questions?</h3>
            <p class="text-text mb-6">Check out the guide page or reach out through GitHub</p>
            <div class="flex flex-wrap justify-center gap-4">
                <NuxtLink
                    to="/guide"
                    class="px-5 py-2.5 bg-highlight-low hover:bg-highlight-med text-foam rounded-md flex items-center transition-colors">
                    <Icon name="lucide:book-open" class="mr-2" />
                    Read the Guide
                </NuxtLink>
                <a
                    href="https://github.com/windbow27/camelia/issues"
                    target="_blank"
                    class="px-5 py-2.5 bg-highlight-low hover:bg-highlight-med text-foam rounded-md flex items-center transition-colors">
                    <Icon name="simple-icons:github" class="mr-2" />
                    Open an Issue
                </a>
            </div>
        </div>
    </div>
</template>

<style scoped>
.transition-all {
    transition-property: all;
}
</style>
