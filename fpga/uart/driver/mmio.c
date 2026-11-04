#define _GNU_SOURCE
#include <stdint.h>
#include <stddef.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <errno.h>

/* This candidate maps only the UART page, never the original adder/DMA bank. */
void *fm_map(uint64_t base, size_t length) {
    if (base != 0x400f0000ULL || length != 4096) { errno=EINVAL; return NULL; }
    int fd=open("/dev/mem",O_RDWR|O_SYNC|O_CLOEXEC);
    if(fd<0) return NULL;
    void *p=mmap(NULL,length,PROT_READ|PROT_WRITE,MAP_SHARED,fd,(off_t)base);
    int saved=errno; close(fd); errno=saved;
    return p==MAP_FAILED ? NULL:p;
}
static inline void barrier(void) {
#if defined(__aarch64__)
    __asm__ __volatile__("dmb sy" ::: "memory");
#else
    __sync_synchronize();
#endif
}
uint32_t fm_read(void *base,uint32_t offset) {
    barrier(); uint32_t value=*(volatile uint32_t *)((uint8_t *)base+offset); barrier(); return value;
}
void fm_write(void *base,uint32_t offset,uint32_t value) {
    barrier(); *(volatile uint32_t *)((uint8_t *)base+offset)=value; barrier();
}
int fm_unmap(void *base,size_t length) { barrier(); return munmap(base,length); }
