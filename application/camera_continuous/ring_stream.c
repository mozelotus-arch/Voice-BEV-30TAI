/* Four-camera, bounded RAM ring. No vehicle/model/audio control. */
#include <libuvc/libuvc.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <string.h>
#include <stdatomic.h>
#include <poll.h>

static volatile sig_atomic_t stopping=0;
static atomic_int publish=0;
static void stop_handler(int sig){(void)sig;stopping=1;}
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
struct camera{int index,node;uvc_device_handle_t *handle;const char *root;unsigned count,bad;};
static void callback(uvc_frame_t *frame,void *ptr){
  struct camera *c=ptr;double stamp=now();
  if(stopping||!atomic_load(&publish))return;
  unsigned char *raw=frame->data;size_t length=frame->data_bytes;
  if(length<4||length>512*1024||frame->width!=320||frame->height!=240||raw[0]!=255||raw[1]!=216){c->bad++;return;}
  unsigned pad=0;while(length>2&&raw[length-1]==0&&pad<7){length--;pad++;}
  if(raw[length-2]!=255||raw[length-1]!=217){c->bad++;return;}
  unsigned index=c->count%8;c->count++;
  char file[1024],temp[1024],meta[1024],metatmp[1024];
  snprintf(file,sizeof(file),"%s/cam%d_%u.jpg",c->root,c->index,index);
  snprintf(temp,sizeof(temp),"%s/cam%d_image.tmp",c->root,c->index);
  snprintf(meta,sizeof(meta),"%s/cam%d.json",c->root,c->index);
  snprintf(metatmp,sizeof(metatmp),"%s/cam%d_meta.tmp",c->root,c->index);
  FILE *f=fopen(temp,"wb");if(!f){stopping=1;return;}
  size_t written=fwrite(raw,1,length,f);int closed=fclose(f);
  if(written!=length||closed||rename(temp,file)){stopping=1;return;}
  f=fopen(metatmp,"w");if(!f){stopping=1;return;}
  fprintf(f,"{\"index\":%d,\"sequence\":%u,\"count\":%u,\"observed_monotonic_s\":%.9f,\"bytes\":%zu,\"file\":\"cam%d_%u.jpg\",\"bad_frames\":%u}\n",c->index,frame->sequence,c->count,stamp,length,c->index,index,c->bad);
  if(fclose(f)||rename(metatmp,meta)){stopping=1;return;}
}
int main(int argc,char **argv){
  if(argc!=6)return 2;
  signal(SIGINT,stop_handler);signal(SIGTERM,stop_handler);setvbuf(stdout,0,_IOLBF,0);
  uvc_context_t *ctx=0;uvc_device_t **list=0;struct camera cameras[4]={0};int rc=0;
  if((rc=uvc_init(&ctx,0)))return 3;
  if((rc=uvc_get_device_list(ctx,&list)))goto cleanup;
  for(int i=0;i<4;i++){
    struct camera *c=&cameras[i];c->index=i;c->root=argv[1];
    const char *colon=strchr(argv[i+2],':');if(!colon){rc=-90;goto cleanup;}
    int address=atoi(argv[i+2]);c->node=atoi(colon+1);
    uvc_device_t *dev=0;for(int j=0;list[j];j++)if(uvc_get_bus_number(list[j])==1&&uvc_get_device_address(list[j])==address){dev=list[j];break;}
    if(!dev){rc=-91;goto cleanup;}
    uvc_device_descriptor_t *desc=0;if((rc=uvc_get_device_descriptor(dev,&desc)))goto cleanup;
    int match=desc->idVendor==0x05a3&&desc->idProduct==0x9230;uvc_free_device_descriptor(desc);if(!match){rc=-92;goto cleanup;}
    if((rc=uvc_open(dev,&c->handle)))goto cleanup;
    uvc_stream_ctrl_t ctrl={0};
    if((rc=uvc_get_stream_ctrl_format_size(c->handle,&ctrl,UVC_FRAME_FORMAT_MJPEG,320,240,0)))goto cleanup;
    ctrl.dwMaxPayloadTransferSize=800;
    if((rc=uvc_start_streaming(c->handle,&ctrl,callback,c,0)))goto cleanup;
  }
  usleep(1000000);atomic_store(&publish,1);
  printf("{\"event\":\"streams_open\",\"warmup_seconds\":1,\"payload_bytes_per_microframe\":800}\n");
  while(!stopping){
    struct pollfd fd={0,POLLIN|POLLHUP,0};int ready=poll(&fd,1,100);
    if(ready>0){char ch;if(read(0,&ch,1)<=0||ch=='Q')break;}
  }
cleanup:
  stopping=1;
  for(int i=0;i<4;i++)if(cameras[i].handle)uvc_stop_streaming(cameras[i].handle);
  for(int i=0;i<4;i++){
    if(cameras[i].handle){
      uvc_close(cameras[i].handle);char node[64];snprintf(node,sizeof(node),"/dev/video%d",cameras[i].node);
      double deadline=now()+3;while(access(node,F_OK)!=0&&now()<deadline)usleep(50000);usleep(300000);
    }
  }
  if(list)uvc_free_device_list(list,1);uvc_exit(ctx);
  printf("{\"event\":\"closed\",\"code\":%d}\n",rc);return rc?4:0;
}
