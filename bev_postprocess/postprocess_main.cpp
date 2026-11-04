// CPU-only adapter around the preserved NuScenes decoder and NMS.
#include "filter.hpp"
#include "nms.hpp"
#include <cmath>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <vector>
#include <iomanip>
std::vector<float> read(const char* path, size_t n) {
    std::ifstream s(path,std::ios::binary); std::vector<float> a(n);
    s.read(reinterpret_cast<char*>(a.data()),n*sizeof(float));
    if(!s || s.peek()!=std::char_traits<char>::eof()) throw std::runtime_error("output length mismatch");
    for(float v:a) if(!std::isfinite(v)) throw std::runtime_error("nonfinite model output");
    return a;
}
int main(int argc,char** argv) {
    try {
        if(argc!=5) return 2;
        auto cls=read(argv[1],800000),box=read(argv[2],720000),dir=read(argv[3],160000);
        for(float s:cls) if(s<0 || s>1) throw std::runtime_error("expected post-sigmoid scores");
        auto candidates=fastbev::filter::threshold_and_decode(cls.data(),box.data(),dir.data(),0.6f);
        for(const auto& b:candidates) if(!std::isfinite(b.x+b.y+b.z+b.w+b.l+b.h+b.yaw+b.vx+b.vy))
            throw std::runtime_error("decoded box overflow");
        fastbev::NMSConfig cfg; cfg.score_thr=.6f;
        cfg.nms_thr_list={.2f,.2f,.2f,.2f,.2f,.2f,.2f,.5f,.5f,.2f};
        auto boxes=fastbev::nms::run_multi_class_nms(candidates,cfg);
        std::ofstream out(argv[4]);out<<std::setprecision(9);
        out<<"{\"schema\":\"FM-FASTBEV-DETECTIONS/1\",\"coordinate_frame\":\"model\",\"threshold\":0.6,\"candidate_count\":"<<candidates.size()<<",\"boxes\":[";
        bool first=true;
        for(const auto& b:boxes) {
            if(!first)out<<","; first=false;
            out<<"{\"x\":"<<b.x<<",\"y\":"<<b.y<<",\"z\":"<<b.z<<",\"w\":"<<b.w<<",\"l\":"<<b.l<<",\"h\":"<<b.h<<",\"yaw\":"<<b.yaw<<",\"vx\":"<<b.vx<<",\"vy\":"<<b.vy<<",\"score\":"<<b.score<<",\"class_id\":"<<b.id<<"}";
        }
        out<<"]}\n";out.close();if(!out) return 3;
        return 0;
    } catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}
}
