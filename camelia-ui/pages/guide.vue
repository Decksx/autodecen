<script setup lang="ts">
const cliInstructions = [
    {
        title: 'Input Image Preparation',
        content: 'Place your input images in the correct directory matching your censorship type',
        code: 'camelia-decensor/input/[model_type]',
        tip: 'Subdirectories are supported for organization',
        icon: 'lucide:folder-input'
    },
    {
        title: 'Run the Command',
        content: 'Execute the main script with your selected model type',
        code: 'python main.py --model_type [model_type]',
        options: ['black_bars', 'white_bars', 'transparent_black', 'mosaic'],
        icon: 'lucide:terminal'
    },
    {
        title: 'Access Results',
        content: 'Find your processed images in the output directory',
        code: 'camelia-decensor/output/',
        icon: 'lucide:folder-output'
    }
];

const webInstructions = [
    {
        title: 'Start API Server',
        content: 'Launch the backend API server first',
        code: 'python api.py',
        icon: 'lucide:server'
    },
    {
        title: 'Start Web Interface',
        content: 'In a separate terminal, launch the web UI',
        code: 'cd camelia-ui\nnpm run dev',
        icon: 'lucide:layout-dashboard'
    },
    {
        title: 'Open in Browser',
        content: 'Access the web interface in your browser',
        code: 'http://localhost:3000',
        icon: 'lucide:globe'
    }
];

const features = [
    {
        title: 'Multiple Censorship Types',
        description: 'Support for black bars, white bars, transparent bars, and mosaics',
        icon: 'lucide:layers'
    },
    {
        title: 'Batch Processing',
        description: 'Process multiple images in one go',
        icon: 'lucide:files'
    },
    {
        title: 'Real-time Logs',
        description: 'Color-coded console output shows processing status',
        icon: 'lucide:activity'
    },
    {
        title: 'Format Support',
        description: 'Handles PNG, JPEG, and WebP image formats',
        icon: 'lucide:image'
    },
    {
        title: 'Side-by-Side Comparison',
        description: 'Compare original and processed images easily',
        icon: 'lucide:split'
    },
    {
        title: 'Bulk Download',
        description: 'Download all results with one click',
        icon: 'lucide:download'
    },
    {
        title: 'Cancellable Jobs',
        description: 'Stop processing at any time',
        icon: 'lucide:x-circle'
    },
    {
        title: 'Directory Support',
        description: 'Maintains subfolder structure for organization',
        icon: 'lucide:folders'
    }
];

const prerequisites = [
    { name: 'Python 3.9', icon: 'logos:python' },
    { name: 'Conda (recommended)', icon: 'logos:conda' },
    { name: 'NVIDIA GPU with CUDA', icon: 'logos:nvidia' },
    { name: 'Node.js 16+', icon: 'logos:nodejs-icon' }
];

const installSteps = [
    {
        title: 'Clone Repository',
        code: 'git clone https://github.com/windbow27/camelia\ncd camelia',
        icon: 'lucide:git-branch'
    },
    {
        title: 'Create Conda Environment',
        code: 'conda create --name camelia_env python=3.9 -y\nconda activate camelia_env',
        icon: 'lucide:terminal'
    },
    {
        title: 'Install Dependencies',
        code: 'pip install -r requirements.txt',
        icon: 'lucide:package'
    },
    {
        title: 'Verify PyTorch with CUDA',
        code: 'python -c "import torch; print(torch.cuda.is_available())"',
        icon: 'lucide:check-circle'
    },
    {
        title: 'Install UI Dependencies',
        code: 'cd camelia-ui\nnpm install\ncd ..',
        icon: 'lucide:terminal'
    }
];
</script>

<template>
    <div class="container">
        <div class="mb-12 text-center">
            <h1 class="text-4xl font-bold text-foam mb-4">Guide</h1>
            <p class="text-text text-lg max-w-3xl mx-auto">
                This guide will help you set up and use the Camelia Decensoring tool. Follow the
                steps below to get started with both CLI and Web UI modes.
            </p>
        </div>

        <!-- Prerequisites Section -->
        <section class="mb-16">
            <div class="mb-6">
                <h2 class="text-2xl font-bold text-iris mb-2">Prerequisites</h2>
                <div class="w-24 h-1 bg-iris"></div>
            </div>

            <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div
                    v-for="(req, index) in prerequisites"
                    :key="index"
                    class="bg-surface p-5 rounded-lg flex flex-col items-center text-center transition-transform hover:scale-105">
                    <Icon :name="req.icon" class="text-5xl mb-3" size="24px" />
                    <h3 class="text-gold">{{ req.name }}</h3>
                </div>
            </div>
        </section>

        <!-- Installation Section -->
        <section class="mb-16">
            <div class="mb-6">
                <h2 class="text-2xl font-bold text-foam mb-2">Installation</h2>
                <div class="w-24 h-1 bg-foam"></div>
            </div>

            <div class="space-y-6">
                <div
                    v-for="(step, index) in installSteps"
                    :key="index"
                    class="bg-surface rounded-lg overflow-hidden">
                    <div class="flex items-center p-4 bg-overlay">
                        <Icon :name="step.icon" class="text-2xl text-foam mr-3" />
                        <h3 class="text-lg font-medium text-foam">
                            {{ index + 1 }}. {{ step.title }}
                        </h3>
                    </div>
                    <pre
                        class="bg-highlight-low p-4 text-text overflow-x-auto"><code>{{ step.code }}</code></pre>
                </div>
            </div>
        </section>

        <!-- Usage Modes Section -->
        <section class="mb-16">
            <div class="mb-8">
                <h2 class="text-2xl font-bold text-gold mb-2">Usage</h2>
                <div class="w-24 h-1 bg-gold"></div>
            </div>

            <!-- CLI Mode -->
            <div class="mb-12">
                <div class="flex items-center mb-6">
                    <div
                        class="w-10 h-10 rounded-full bg-base flex items-center justify-center border-2 border-rose">
                        <Icon name="lucide:terminal" class="text-xl text-rose" />
                    </div>
                    <h3 class="text-xl font-bold text-rose ml-3">CLI Mode</h3>
                </div>

                <div class="flex flex-col space-y-6">
                    <div
                        v-for="(step, index) in cliInstructions"
                        :key="index"
                        class="bg-surface rounded-lg overflow-hidden">
                        <div class="flex items-center bg-highlight-low p-4">
                            <div
                                class="bg-base rounded-full w-8 h-8 flex items-center justify-center mr-4 text-rose">
                                {{ index + 1 }}
                            </div>
                            <div>
                                <h4 class="text-lg font-medium text-rose">{{ step.title }}</h4>
                                <p class="text-subtle">{{ step.content }}</p>
                            </div>
                            <Icon :name="step.icon" class="ml-auto text-2xl text-subtle" />
                        </div>
                        <div class="p-4">
                            <pre
                                class="bg-highlight-med p-3 rounded text-pine overflow-x-auto"><code>{{ step.code }}</code></pre>

                            <div v-if="step.options" class="mt-3 flex flex-wrap gap-2">
                                <span
                                    v-for="option in step.options"
                                    :key="option"
                                    class="px-2 py-1 bg-overlay rounded-md text-sm text-gold">
                                    {{ option }}
                                </span>
                            </div>

                            <div v-if="step.tip" class="mt-3 text-sm text-iris italic">
                                <Icon name="lucide:lightbulb" class="inline mr-1" />
                                {{ step.tip }}
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Web UI Mode -->
            <div>
                <div class="flex items-center mb-6">
                    <div
                        class="w-10 h-10 rounded-full bg-base flex items-center justify-center border-2 border-iris">
                        <Icon name="lucide:layout-dashboard" class="text-xl text-iris" />
                    </div>
                    <h3 class="text-xl font-bold text-iris ml-3">Web UI Mode</h3>
                </div>

                <div class="flex flex-col space-y-6">
                    <div
                        v-for="(step, index) in webInstructions"
                        :key="index"
                        class="bg-surface rounded-lg overflow-hidden">
                        <div class="flex items-center bg-highlight-low p-4">
                            <div
                                class="bg-base rounded-full w-8 h-8 flex items-center justify-center mr-4 text-iris">
                                {{ index + 1 }}
                            </div>
                            <div>
                                <h4 class="text-lg font-medium text-iris">{{ step.title }}</h4>
                                <p class="text-subtle">{{ step.content }}</p>
                            </div>
                            <Icon :name="step.icon" class="ml-auto text-2xl text-subtle" />
                        </div>
                        <div class="p-4">
                            <pre
                                class="bg-highlight-med p-3 rounded text-pine overflow-x-auto"><code>{{ step.code }}</code></pre>
                        </div>
                    </div>

                    <div class="bg-surface rounded-lg overflow-hidden">
                        <div class="p-6 space-y-4">
                            <h4 class="text-lg font-medium text-iris">Using the Web Interface</h4>

                            <div class="flex items-start space-x-3">
                                <div
                                    class="w-6 h-6 rounded-full bg-iris flex items-center justify-center text-xs text-base flex-shrink-0">
                                    1
                                </div>
                                <p class="text-text">Upload your images using the file uploader</p>
                            </div>

                            <div class="flex items-start space-x-3">
                                <div
                                    class="w-6 h-6 rounded-full bg-iris flex items-center justify-center text-xs text-base flex-shrink-0">
                                    2
                                </div>
                                <p class="text-text">
                                    Select the desired processing type (Black Bars, White Bars, or
                                    Transparent Black)
                                </p>
                            </div>

                            <div class="flex items-start space-x-3">
                                <div
                                    class="w-6 h-6 rounded-full bg-iris flex items-center justify-center text-xs text-base flex-shrink-0">
                                    3
                                </div>
                                <p class="text-text">Click "Process Images" to begin processing</p>
                            </div>

                            <div class="flex items-start space-x-3">
                                <div
                                    class="w-6 h-6 rounded-full bg-iris flex items-center justify-center text-xs text-base flex-shrink-0">
                                    4
                                </div>
                                <p class="text-text">View real-time logs in the console display</p>
                            </div>

                            <div class="flex items-start space-x-3">
                                <div
                                    class="w-6 h-6 rounded-full bg-iris flex items-center justify-center text-xs text-base flex-shrink-0">
                                    5
                                </div>
                                <p class="text-text">
                                    Once complete, view and download the results
                                </p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- Features Section -->
        <section class="mb-16">
            <div class="mb-6">
                <h2 class="text-2xl font-bold text-pine mb-2">Features</h2>
                <div class="w-24 h-1 bg-pine"></div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                <div
                    v-for="feature in features"
                    :key="feature.title"
                    class="bg-highlight-low rounded-lg p-5 transition-all hover:bg-highlight-med">
                    <div class="flex items-center mb-3">
                        <div
                            class="w-10 h-10 rounded-full bg-base flex items-center justify-center">
                            <Icon :name="feature.icon" class="text-pine" />
                        </div>
                        <h3 class="ml-3 font-medium text-foam">{{ feature.title }}</h3>
                    </div>
                    <p class="text-subtle text-sm">{{ feature.description }}</p>
                </div>
            </div>
        </section>
    </div>
</template>

<style scoped>
code {
    font-family: 'Courier New', Courier, monospace;
    white-space: pre;
}

pre {
    font-size: 0.9em;
}
</style>
