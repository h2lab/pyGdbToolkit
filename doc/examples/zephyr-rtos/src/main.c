/*
 * SPDX-FileCopyrightText: 2026 H2Lab Development Team
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdint.h>
#include <zephyr/kernel.h>

K_SEM_DEFINE(validation_sem, 0, 1);
K_MUTEX_DEFINE(validation_mutex);
K_EVENT_DEFINE(validation_event);

volatile uint32_t validation_cycles;

static void sem_waiter(void *unused1, void *unused2, void *unused3)
{
	(void)unused1;
	(void)unused2;
	(void)unused3;
	(void)k_sem_take(&validation_sem, K_FOREVER);
	k_panic();
}

static void mutex_waiter(void *unused1, void *unused2, void *unused3)
{
	(void)unused1;
	(void)unused2;
	(void)unused3;
	(void)k_mutex_lock(&validation_mutex, K_FOREVER);
	k_panic();
}

static void event_waiter(void *all, void *unused2, void *unused3)
{
	(void)unused2;
	(void)unused3;
	if (all != NULL) {
		(void)k_event_wait_all(&validation_event, 0x6, false, K_FOREVER);
	} else {
		(void)k_event_wait(&validation_event, 0x6, false, K_FOREVER);
	}
	k_panic();
}

static void sleeper(void *unused1, void *unused2, void *unused3)
{
	volatile uint32_t stack_data[32];

	(void)unused1;
	(void)unused2;
	(void)unused3;
	for (uint32_t i = 0; i < ARRAY_SIZE(stack_data); ++i) {
		stack_data[i] = i;
	}
	for (;;) {
		k_sleep(K_SECONDS(30));
		++stack_data[0];
	}
}

static void suspended(void *unused1, void *unused2, void *unused3)
{
	(void)unused1;
	(void)unused2;
	(void)unused3;
	k_thread_suspend(k_current_get());
	k_panic();
}

K_THREAD_DEFINE(validation_sem_thread, 1024, sem_waiter, NULL, NULL, NULL, 2, 0, 0);
K_THREAD_DEFINE(validation_mutex_thread, 1024, mutex_waiter, NULL, NULL, NULL, 2, 0, 0);
K_THREAD_DEFINE(validation_event_any, 1024, event_waiter, NULL, NULL, NULL, 2, 0, 0);
K_THREAD_DEFINE(validation_event_all, 1024, event_waiter, (void *)1, NULL, NULL, 2, 0, 0);
K_THREAD_DEFINE(validation_sleeper, 1024, sleeper, NULL, NULL, NULL, 3, 0, 0);
K_THREAD_DEFINE(validation_suspended, 1024, suspended, NULL, NULL, NULL, 3, 0, 0);

/* Stable Thread-mode stop after every lower-priority worker has blocked. */
__attribute__((noinline)) void validation_checkpoint(void)
{
	__asm__ volatile("" ::: "memory");
}

int main(void)
{
	k_thread_name_set(k_current_get(), "validation_main");
	if (k_mutex_lock(&validation_mutex, K_FOREVER) != 0 ||
	    k_mutex_lock(&validation_mutex, K_FOREVER) != 0) {
		k_panic();
	}
	k_event_post(&validation_event, 0x1);
	for (;;) {
		k_msleep(50);
		++validation_cycles;
		validation_checkpoint();
	}
}
