"""Render actual decoded model detections on the board; no camera stitching."""
import cv2
import numpy as np

CLASSES=('car','truck','trailer','bus','construction','bicycle','motorcycle','pedestrian','cone','barrier')

def box_corners(b):
    c,s=np.cos(b['yaw']),np.sin(b['yaw'])
    local=np.array([[-b['w']/2,-b['l']/2],[b['w']/2,-b['l']/2],
                    [b['w']/2,b['l']/2],[-b['w']/2,b['l']/2]])
    # visualize.cpp compute_corners: rx=lx*cos-ly*sin, ry=lx*sin+ly*cos.
    return local@np.array([[c,s],[-s,c]])+np.array([b['x'],b['y']])

def render(path, detections, frame_id, active):
    image=np.full((900,900,3),24,np.uint8)
    def point(x,y): return (int(round(450+x*7.2)), int(round(450-y*7.2)))
    for m in range(-50,51,10):
        cv2.line(image,point(m,-50),point(m,50),(55,55,55),1)
        cv2.line(image,point(-50,m),point(50,m),(55,55,55),1)
        cv2.putText(image,str(m)+'m',point(m,-52),cv2.FONT_HERSHEY_SIMPLEX,.35,(150,150,150),1)
    cv2.arrowedLine(image,point(0,0),point(0,5),(255,255,255),2)
    cv2.circle(image,point(0,0),4,(255,255,255),-1)
    for b in detections['boxes']:
        # Preserve the original visualizer's rectangle convention, model axes.
        xy=box_corners(b)
        pts=np.array([point(x,y) for x,y in xy],np.int32)
        cv2.polylines(image,[pts],True,(50,205,255),1,cv2.LINE_AA)
        if -50<=b['x']<=50 and -50<=b['y']<=50:
            cv2.putText(image,CLASSES[b['class_id']]+(' %.2f'%b['score']),point(b['x'],b['y']),cv2.FONT_HERSHEY_SIMPLEX,.32,(100,230,255),1)
    lines=['FastBEV model detections | model X right / Y forward',str(frame_id)[:95],
           'Actual slots: '+','.join(map(str,active))+' | static serial snapshots',
           'Candidate calibration; partial views; geometry/accuracy NOT validated',
           'Detections: %d | score > 0.60 | no fabricated boxes'%len(detections['boxes'])]
    for i,line in enumerate(lines):
        cv2.putText(image,line,(18,24+i*20),cv2.FONT_HERSHEY_SIMPLEX,.43,(230,230,230),1,cv2.LINE_AA)
    cv2.putText(image,'BEV detection plot, not a stitched camera photograph',(18,877),cv2.FONT_HERSHEY_SIMPLEX,.45,(210,210,210),1)
    if not cv2.imwrite(str(path),image):raise RuntimeError('board_png_write_failed')
