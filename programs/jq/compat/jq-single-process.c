/*
 * Single-process pthread subset for the jq command-line comparison subject.
 *
 * jq uses pthread_once and pthread keys for lazy process-local state.  Its
 * --run-tests route also starts three independent workers.  The measured
 * runtime does not provide those calls.  This adapter preserves process-local
 * initialization and executes those bounded workers sequentially with
 * isolated key values in both the LLVM and native-observer builds; it is
 * deliberately not a general pthread implementation.
 */

#include <errno.h>
#include <pthread.h>
#include <stddef.h>

#define JQ_ADAPTER_MAX_ONCE_CONTROLS 16
#define JQ_ADAPTER_MAX_KEYS 16
#define JQ_ADAPTER_MAX_THREADS 16
#define JQ_ADAPTER_DESTRUCTOR_ITERATIONS 4

static const pthread_once_t *jq_once_controls[JQ_ADAPTER_MAX_ONCE_CONTROLS];
static size_t jq_once_control_count;

static unsigned char jq_key_active[JQ_ADAPTER_MAX_KEYS];
static void (*jq_key_destructors[JQ_ADAPTER_MAX_KEYS])(void *);
static void *jq_key_values[JQ_ADAPTER_MAX_THREADS][JQ_ADAPTER_MAX_KEYS];
static unsigned char jq_thread_active[JQ_ADAPTER_MAX_THREADS];
static unsigned char jq_thread_joined[JQ_ADAPTER_MAX_THREADS];
static void *jq_thread_results[JQ_ADAPTER_MAX_THREADS];
static size_t jq_current_thread;

static void
jq_run_key_destructors(size_t thread_index) {
  size_t iteration;
  size_t key_index;

  for (iteration = 0; iteration < JQ_ADAPTER_DESTRUCTOR_ITERATIONS;
       iteration++) {
    int called = 0;
    for (key_index = 0; key_index < JQ_ADAPTER_MAX_KEYS; key_index++) {
      void *value = jq_key_values[thread_index][key_index];
      void (*destructor)(void *) = jq_key_destructors[key_index];
      if (!jq_key_active[key_index] || value == NULL || destructor == NULL)
        continue;
      jq_key_values[thread_index][key_index] = NULL;
      destructor(value);
      called = 1;
    }
    if (!called)
      return;
  }
}

int
pthread_once(pthread_once_t *once_control, void (*init_routine)(void)) {
  size_t index;

  if (once_control == NULL || init_routine == NULL)
    return EINVAL;
  for (index = 0; index < jq_once_control_count; index++) {
    if (jq_once_controls[index] == once_control)
      return 0;
  }
  if (jq_once_control_count == JQ_ADAPTER_MAX_ONCE_CONTROLS)
    return EAGAIN;

  jq_once_controls[jq_once_control_count++] = once_control;
  init_routine();
  return 0;
}

int
pthread_key_create(pthread_key_t *key, void (*destructor)(void *)) {
  size_t index;

  if (key == NULL)
    return EINVAL;
  for (index = 0; index < JQ_ADAPTER_MAX_KEYS; index++) {
    if (jq_key_active[index])
      continue;
    jq_key_active[index] = 1;
    jq_key_destructors[index] = destructor;
    *key = (pthread_key_t)index;
    return 0;
  }
  return EAGAIN;
}

int
pthread_setspecific(pthread_key_t key, const void *value) {
  const size_t index = (size_t)key;
  if (index >= JQ_ADAPTER_MAX_KEYS || !jq_key_active[index])
    return EINVAL;
  jq_key_values[jq_current_thread][index] = (void *)value;
  return 0;
}

void *
pthread_getspecific(pthread_key_t key) {
  const size_t index = (size_t)key;
  if (index >= JQ_ADAPTER_MAX_KEYS || !jq_key_active[index])
    return NULL;
  return jq_key_values[jq_current_thread][index];
}

int
pthread_key_delete(pthread_key_t key) {
  const size_t index = (size_t)key;
  size_t thread_index;

  if (index >= JQ_ADAPTER_MAX_KEYS || !jq_key_active[index])
    return EINVAL;
  jq_key_active[index] = 0;
  jq_key_destructors[index] = NULL;
  for (thread_index = 0; thread_index < JQ_ADAPTER_MAX_THREADS;
       thread_index++)
    jq_key_values[thread_index][index] = NULL;
  return 0;
}

int
pthread_create(pthread_t *restrict thread,
               const pthread_attr_t *restrict attributes,
               void *(*start_routine)(void *), void *restrict argument) {
  size_t thread_index;
  size_t previous_thread;

  if (thread == NULL || start_routine == NULL)
    return EINVAL;
  if (attributes != NULL)
    return ENOTSUP;
  for (thread_index = 1; thread_index < JQ_ADAPTER_MAX_THREADS;
       thread_index++) {
    if (!jq_thread_active[thread_index])
      break;
  }
  if (thread_index == JQ_ADAPTER_MAX_THREADS)
    return EAGAIN;

  jq_thread_active[thread_index] = 1;
  jq_thread_joined[thread_index] = 0;
  *thread = (pthread_t)thread_index;
  previous_thread = jq_current_thread;
  jq_current_thread = thread_index;
  jq_thread_results[thread_index] = start_routine(argument);
  jq_run_key_destructors(thread_index);
  jq_current_thread = previous_thread;
  return 0;
}

int
pthread_join(pthread_t thread, void **result) {
  const size_t thread_index = (size_t)thread;

  if (thread_index == 0 || thread_index >= JQ_ADAPTER_MAX_THREADS ||
      !jq_thread_active[thread_index] || jq_thread_joined[thread_index])
    return ESRCH;
  if (result != NULL)
    *result = jq_thread_results[thread_index];
  jq_thread_joined[thread_index] = 1;
  jq_thread_active[thread_index] = 0;
  jq_thread_results[thread_index] = NULL;
  return 0;
}
