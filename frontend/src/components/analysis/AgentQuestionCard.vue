<template>
  <section class="question-card" aria-labelledby="question-card-title">
    <div class="question-card__header">
      <div>
        <h2 id="question-card-title">{{ currentQuestion?.prompt }}</h2>
      </div>
      <span class="question-card__count">{{ currentIndex + 1 }} / {{ pendingInput.questions.length }}</span>
    </div>

    <div v-if="currentQuestion && (currentQuestion.options.length || currentQuestion.allow_custom)" class="options" role="group" aria-label="可选答案">
      <button
        v-for="(option, index) in currentQuestion.options"
        :key="option.id"
        type="button"
        class="option-button answer-row"
        :class="{ 'option-button--selected': selectedAnswers.includes(option.value) }"
        :disabled="busy"
        @click="selectOption(option.value)"
      >
        <span class="answer-row__mark option-button__mark">{{ choiceLabel(index) }}</span>
        <span>{{ option.label }}</span>
      </button>
      <label v-if="currentQuestion.allow_custom" class="custom-answer answer-row" :class="{ 'option-button--selected': Boolean(customAnswer.trim()) }">
        <span class="answer-row__mark custom-answer__mark">{{ choiceLabel(currentQuestion.options.length) }}</span>
        <span class="custom-answer__content">
          <input type="text" :value="customAnswer" maxlength="500" placeholder="请输入自己的答案" :disabled="busy" @input="setCustomAnswer" />
        </span>
      </label>
    </div>

    <Transition name="state-fade"><p v-if="error || submitError" class="question-card__error" role="alert">{{ error || submitError }}</p></Transition>
    <div class="question-card__actions">
      <button type="button" class="button button--quiet" :disabled="currentIndex === 0 || busy" aria-label="上一题" @click="previous">上一步</button>
      <button v-if="!isLast" type="button" class="button button--primary" :disabled="busy" aria-label="下一题" @click="next">下一题 <ArrowRight :size="16" aria-hidden="true" /></button>
      <button v-else type="button" class="button button--primary" :disabled="busy" aria-label="提交回答" @click="submit">{{ busy ? '正在提交…' : '提交回答' }} <ArrowUpRight :size="16" aria-hidden="true" /></button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowRight, ArrowUpRight } from 'lucide-vue-next'
import type { AgentAnswer, PendingInput } from '../../types/analysis'
import type { PendingQuestionDraft } from '../../types/conversation'

const props = defineProps<{ pendingInput: PendingInput; busy?: boolean; submitError?: string; identity?: string; draft?: PendingQuestionDraft | null }>()
const emit = defineEmits<{ submit: [answers: AgentAnswer[]]; 'update:draft': [draft: PendingQuestionDraft] }>()

const currentIndex = ref(0)
const selectedAnswerMap = ref<Record<string, string[]>>({})
const customAnswerMap = ref<Record<string, string>>({})
const error = ref('')
const currentQuestion = computed(() => props.pendingInput.questions[currentIndex.value])
const identity = computed(() => props.identity || `${props.pendingInput.tool_call_id}:${props.pendingInput.resumeRevision ?? 0}:${props.pendingInput.questions.map((question) => question.id).join(',')}`)
const selectedAnswers = computed(() => currentQuestion.value ? selectedAnswerMap.value[currentQuestion.value.id] || [] : [])
const customAnswer = computed(() => currentQuestion.value ? customAnswerMap.value[currentQuestion.value.id] || '' : '')
const currentAnswers = computed(() => [
  ...selectedAnswers.value,
  ...(customAnswer.value.trim() ? [customAnswer.value.trim()] : []),
])
const isLast = computed(() => currentIndex.value === props.pendingInput.questions.length - 1)
const busy = computed(() => Boolean(props.busy))

function restoreDraft(draft?: PendingQuestionDraft | null) {
  if (!draft || draft.identity !== identity.value) {
    currentIndex.value = 0
    selectedAnswerMap.value = {}
    customAnswerMap.value = {}
  } else {
    currentIndex.value = Math.min(Math.max(0, draft.questionIndex), Math.max(0, props.pendingInput.questions.length - 1))
    selectedAnswerMap.value = { ...draft.answers }
    customAnswerMap.value = { ...draft.customValues }
  }
  error.value = ''
}

function persistDraft() {
  emit('update:draft', {
    identity: identity.value,
    questionIndex: currentIndex.value,
    answers: { ...selectedAnswerMap.value },
    customValues: { ...customAnswerMap.value },
  })
}

watch(identity, () => restoreDraft(props.draft), { immediate: true })
watch(() => props.draft, (draft) => {
  if (draft?.identity === identity.value) restoreDraft(draft)
}, { deep: true })

function selectOption(value: string) {
  if (busy.value || !currentQuestion.value) return
  const question = currentQuestion.value
  const selected = selectedAnswers.value
  if (question.selection_mode === 'multiple') {
    if (selected.includes(value)) {
      selectedAnswerMap.value[question.id] = selected.filter((item) => item !== value)
    } else if (selected.length + (customAnswer.value.trim() ? 1 : 0) < question.max_selections) {
      selectedAnswerMap.value[question.id] = [...selected, value]
    } else {
      error.value = `最多选择 ${question.max_selections} 项。`
      return
    }
  } else {
    selectedAnswerMap.value[question.id] = [value]
    customAnswerMap.value[question.id] = ''
  }
  error.value = ''
  persistDraft()
}

function setCustomAnswer(event: Event) {
  if (busy.value || !currentQuestion.value) return
  const question = currentQuestion.value
  const value = (event.target as HTMLInputElement).value
  customAnswerMap.value[question.id] = value
  if (question.selection_mode !== 'multiple') selectedAnswerMap.value[question.id] = []
  error.value = ''
  persistDraft()
}

function validateCurrent(): boolean {
  if (!currentQuestion.value) return false
  if (currentQuestion.value.required && !currentAnswers.value.length) {
    error.value = '请先回答这个问题。'
    return false
  }
  return true
}

function next() {
  if (busy.value) return
  if (!validateCurrent()) return
  error.value = ''
  currentIndex.value += 1
  persistDraft()
}

function previous() {
  if (busy.value) return
  if (currentIndex.value > 0) currentIndex.value -= 1
  error.value = ''
  persistDraft()
}

function submit() {
  if (busy.value) return
  if (!validateCurrent()) return
  const answers: AgentAnswer[] = []
  for (const question of props.pendingInput.questions) {
    const values = [
      ...(selectedAnswerMap.value[question.id] || []),
      ...(customAnswerMap.value[question.id]?.trim() ? [customAnswerMap.value[question.id].trim()] : []),
    ].filter((value, index, all) => all.indexOf(value) === index)
    if (question.required && !values.length) {
      currentIndex.value = props.pendingInput.questions.findIndex((item) => item.id === question.id)
      error.value = '请先回答这个问题。'
      return
    }
    if (values.length > question.max_selections) {
      currentIndex.value = props.pendingInput.questions.findIndex((item) => item.id === question.id)
      error.value = `最多选择 ${question.max_selections} 项。`
      return
    }
    if (values.length) answers.push({ question_id: question.id, values })
  }
  emit('submit', answers)
}

function choiceLabel(index: number): string {
  return String.fromCharCode('A'.charCodeAt(0) + index)
}
</script>

<style scoped>
.question-card { padding: clamp(20px, 4vw, 30px); border: 1px solid rgb(194 65 12 / 0.24); border-radius: 16px; background: linear-gradient(135deg, #fff 0%, var(--color-orange-soft) 100%); }
.question-card__header, .question-card__actions { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
h2 { margin: 0; color: var(--color-navy); font-size: clamp(20px, 3vw, 27px); letter-spacing: -0.045em; }
.question-card__count { color: var(--color-orange); font-family: var(--font-mono); font-size: 12px; font-weight: 700; }
.options { display: grid; gap: 9px; margin-top: 24px; }
.answer-row { display: flex; align-items: center; gap: 11px; min-height: 48px; padding: 8px 13px; color: var(--color-navy); border: 1px solid rgb(15 45 70 / 0.12); border-radius: 11px; background: #fff; font-size: 14px; line-height: 1.5; text-align: left; overflow-wrap: anywhere; }
.option-button { width: 100%; }
.option-button--selected { border-color: var(--color-teal); background: var(--color-teal-soft); box-shadow: 0 0 0 2px rgb(15 118 110 / 0.1); }
.answer-row__mark { display: grid; flex: 0 0 25px; width: 25px; height: 25px; place-items: center; color: var(--color-orange); border: 1px solid currentColor; border-radius: 7px; font-family: var(--font-mono); font-size: 11px; font-weight: 700; }
.custom-answer { cursor: text; color: var(--color-ink-muted); }
.custom-answer__content { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
.custom-answer input { min-width: 0; min-height: 42px; flex: 1; padding: 0 10px; color: var(--color-navy); border: 1px solid var(--color-border-strong); border-radius: 8px; background: #fff; font: inherit; font-size: 14px; }
.question-card__error { margin: 14px 0 0; color: var(--color-danger); font-size: 12px; }
.question-card__actions { align-items: center; justify-content: flex-end; margin-top: 24px; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 44px; padding: 0 14px; border: 1px solid transparent; border-radius: 10px; font-size: 13px; font-weight: 750; }
.button:disabled { cursor: not-allowed; opacity: 0.45; }
.button--primary { color: #fff; background: var(--color-navy); }
.button--quiet { color: var(--color-ink-soft); border-color: var(--color-border); background: #fff; }
@media (max-width: 520px) { .question-card__actions { align-items: stretch; flex-direction: column; } .button { width: 100%; } }
</style>
