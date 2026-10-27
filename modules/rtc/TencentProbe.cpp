// Measurement-only client using the official Tencent TRTC Windows C++ SDK.
// No windows, microphone, camera, sound-card playback, aircraft or local tools.
// Config contains only short-lived UserSig; long-term keys stay in Python memory.
// stdin JSONL: {"command":"play"}, {"command":"stop"}; stdout: JSONL only.
// Build against official Win64 liteav.lib and nlohmann/json 3.12.0 (MIT).
#define NOMINMAX
#include <windows.h>
#include "TRTC/ITRTCCloud.h"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;
using namespace liteav;
static const auto origin = Clock::now();
static std::mutex outputMutex;
static std::atomic<bool> stopping{false};
static double nowMs() { return std::chrono::duration<double,std::milli>(Clock::now()-origin).count(); }
static void emit(const std::string& event,json fields=json::object()) {
    fields["event"]=event;fields["monotonic_ms"]=nowMs();
    std::lock_guard<std::mutex> guard(outputMutex);
    std::cout<<fields.dump(-1,' ',false,json::error_handler_t::replace)<<'\n'<<std::flush;
}
static std::vector<uint8_t> readBytes(const std::string& path,size_t maxBytes) {
    std::ifstream in(path,std::ios::binary|std::ios::ate);
    if(!in)throw std::runtime_error("cannot open media file");
    auto size=in.tellg();if(size<=0||static_cast<uint64_t>(size)>maxBytes)throw std::runtime_error("invalid media size");
    std::vector<uint8_t> value(static_cast<size_t>(size));in.seekg(0);in.read(reinterpret_cast<char*>(value.data()),size);
    if(!in)throw std::runtime_error("incomplete media file");return value;
}
struct Level { double rms;int peak; };
static Level level(const uint8_t* bytes,size_t size) {
    size_t samples=size/2;double sum=0;int peak=0;
    for(size_t i=0;i<samples;i++){int16_t value;std::memcpy(&value,bytes+i*2,2);sum+=double(value)*value;peak=std::max(peak,std::abs(int(value)));}
    return {samples?std::sqrt(sum/samples):0,peak};
}
static void pumpMessages() {
    MSG message;while(PeekMessage(&message,nullptr,0,0,PM_REMOVE)){TranslateMessage(&message);DispatchMessage(&message);}
}
static bool stdinLines(std::string& pending,std::vector<std::string>& lines) {
    HANDLE handle=GetStdHandle(STD_INPUT_HANDLE);DWORD available=0;
    if(!PeekNamedPipe(handle,nullptr,0,nullptr,&available,nullptr)){
        if(GetLastError()==ERROR_BROKEN_PIPE)return false;
        throw std::runtime_error("probe stdin must be a pipe");
    }
    if(available){char buffer[4096];DWORD count=0;
        if(!ReadFile(handle,buffer,std::min<DWORD>(available,sizeof(buffer)),&count,nullptr)||!count)return false;
        pending.append(buffer,count);if(pending.size()>65536)throw std::runtime_error("stdin command too large");
        size_t end;while((end=pending.find('\n'))!=std::string::npos){auto line=pending.substr(0,end);pending.erase(0,end+1);if(!line.empty())lines.push_back(line);}
    }
    return true;
}
class Handler final:public ITRTCCloudCallback {
public:
    std::string bot;
    std::atomic<bool> joined{false},botReady{false},exited{false};
    explicit Handler(std::string user):bot(std::move(user)){}
    void onError(TXLiteAVError code,const char*,void*) override {emit("sdk_error",{{"code",int(code)}});stopping=true;}
    void onWarning(TXLiteAVWarning code,const char*,void*) override {emit("sdk_warning",{{"code",int(code)}});}
    void onEnterRoom(int result) override {joined=result>0;emit(joined?"room_joined":"room_state",{{"state",result}});if(result<0)stopping=true;}
    void onExitRoom(int reason) override {joined=false;exited=true;emit("room_exited",{{"reason",reason}});}
    void onRemoteUserEnterRoom(const char* id) override {if(id&&bot==id){botReady=true;emit("bot_ready");}}
    void onRemoteUserLeaveRoom(const char* id,int reason) override {if(id&&bot==id){botReady=false;emit("bot_left",{{"reason",reason}});}}
    void onUserAudioAvailable(const char* id,bool available) override {if(id&&bot==id)emit("remote_audio_publish",{{"published",available}});}
    void onFirstAudioFrame(const char* id) override {if(id&&bot==id)emit("remote_audio_first_frame_callback");}
    void onRecvCustomCmdMsg(const char* id,int32_t command,uint32_t sequence,const uint8_t* bytes,uint32_t size) override {
        if(!id||bot!=id){emit("ignored_sender");return;}
        if(size>65536){emit("malformed_message",{{"bytes",size}});return;}
        try{
            auto value=json::parse(bytes,bytes+size);
            // Official subtitle/state messages are JSON, not ByteRTC TLV.
            emit("custom_message",{{"cmd_id",command},{"sequence",sequence},{"payload",value}});
        }catch(const json::exception&){emit("malformed_message",{{"bytes",size}});}
    }
};
static int selfTest() {
    const int16_t samples[]={0,-1000,1000,0};auto value=level(reinterpret_cast<const uint8_t*>(samples),sizeof(samples));
    if(value.peak!=1000||std::abs(value.rms-std::sqrt(500000.0))>.001)throw std::runtime_error("PCM level calculation failed");
    emit("self_test",{{"ok",true},{"network",false},{"devices",false}});return 0;
}
int main(int argc,char** argv) {
    if(argc==2&&std::string(argv[1])=="--self-test")return selfTest();
    ITRTCCloud* engine=nullptr;std::unique_ptr<Handler> handler;
    try{
        if(argc!=2)throw std::runtime_error("usage: TencentProbe config.json");
        std::ifstream input(argv[1]);if(!input)throw std::runtime_error("cannot read config");
        json config;input>>config;
        uint32_t appId=0;
        auto app=config.contains("sdkAppId")?config.at("sdkAppId"):config.at("appId");
        if(app.is_string())appId=static_cast<uint32_t>(std::stoul(app.get<std::string>()));else appId=app.get<uint32_t>();
        const std::string token=config.value("userSig",config.value("token",std::string())),room=config.at("roomId"),user=config.at("userId"),bot=config.at("botId");
        if(!appId||token.empty()||room.empty()||user.empty()||bot.empty())throw std::runtime_error("missing room credentials");
        int rate=config.value("sampleRate",16000),width=config.value("width",640),height=config.value("height",360),fps=config.value("fps",2),maxSeconds=config.value("maxSeconds",45);
        double threshold=config.value("rmsThreshold",64.0);
        if((rate!=16000&&rate!=48000)||width<2||height<2||width>1920||height>1080||width%2||height%2||fps<1||fps>30||maxSeconds<1||maxSeconds>180||!std::isfinite(threshold)||threshold<1||threshold>32000)
            throw std::runtime_error("invalid probe parameters");
        auto pcm=readBytes(config.at("pcmPath"),rate*2*30);if(pcm.size()%2)throw std::runtime_error("PCM must be mono S16LE");
        std::vector<uint8_t> image;
        if(config.contains("imagePath")&&!config["imagePath"].get<std::string>().empty()){
            image=readBytes(config["imagePath"],width*height*3/2);
            if(image.size()!=size_t(width*height*3/2))throw std::runtime_error("I420 size mismatch");
        }
        std::ofstream output;
        if(config.contains("outputPcmPath")){output.open(config["outputPcmPath"].get<std::string>(),std::ios::binary);if(!output)throw std::runtime_error("cannot write output PCM");}
        size_t lastVoiceByte=0;
        for(size_t i=0;i+1<pcm.size();i+=2){int16_t value;std::memcpy(&value,pcm.data()+i,2);if(std::abs(int(value))>=threshold)lastVoiceByte=i+2;}
        if(config.contains("speechEndSamples")){auto sample=config["speechEndSamples"].get<size_t>();if(!sample||sample>pcm.size()/2)throw std::runtime_error("invalid speech endpoint");lastVoiceByte=sample*2;}
        handler=std::make_unique<Handler>(bot);engine=getTRTCShareInstance();if(!engine)throw std::runtime_error("cannot create TRTC engine");
        engine->setConsoleEnabled(false);engine->setLogLevel(TRTCLogLevelNone);engine->showDebugView(0);
        engine->addCallback(handler.get());engine->enableCustomAudioCapture(true);engine->enableCustomAudioRendering(true);
        if(!image.empty()){
            engine->enableCustomVideoCapture(TRTCVideoStreamTypeBig,true);
            TRTCVideoEncParam video;video.videoResolution=TRTCVideoResolution_640_360;video.videoFps=fps;video.videoBitrate=400;
            engine->setVideoEncoderParam(video);
        }
        TRTCParams params;params.sdkAppId=appId;params.userId=user.c_str();params.userSig=token.c_str();params.roomId=0;params.strRoomId=room.c_str();
        emit("probe_start",{{"route","TencentTRTC"},{"client","official_windows_native"},{"sdk_version",engine->getSDKVersion()},{"audio_rate",rate},{"audio_bytes",pcm.size()},{"hardware",false},{"audio_sink","external_pull_48k_mono_s16le"},{"video_understanding_verified",false}});
        emit("join_requested");engine->enterRoom(params,image.empty()?TRTCAppSceneAudioCall:TRTCAppSceneVideoCall);
        std::vector<uint8_t> inputFrame(rate/50*2),outputFrame(960*2);
        size_t offset=0;uint64_t outputSamples=0,videoCount=0,ticks=0;
        bool playing=false,inputEnded=false,firstOutput=false;double playMs=0,candidateMs=0;int loudFrames=0;
        auto next=Clock::now();const auto deadline=Clock::now()+std::chrono::seconds(maxSeconds);std::string pending;
        while(!stopping&&Clock::now()<deadline){
            pumpMessages();std::vector<std::string> lines;
            if(!stdinLines(pending,lines)){stopping=true;break;}
            for(const auto& line:lines){
                auto command=json::parse(line);std::string kind=command.at("command");
                if(kind=="stop"){stopping=true;break;}
                if(kind!="play")throw std::runtime_error("unsupported Tencent probe command; no native client tool channel verified");
                if(playing||!handler->joined||!handler->botReady)throw std::runtime_error("play requires joined bot, once per session");
                playing=true;playMs=nowMs();emit("audio_input_started",{{"speech_endpoint_offset_ms",double(lastVoiceByte)*1000/(rate*2)},{"pcm_duration_ms",double(pcm.size())*1000/(rate*2)}});
            }
            if(stopping)break;
            if(handler->joined){
                std::fill(inputFrame.begin(),inputFrame.end(),0);
                if(playing&&!inputEnded){size_t count=std::min(inputFrame.size(),pcm.size()-offset);std::copy_n(pcm.data()+offset,count,inputFrame.data());offset+=count;}
                TRTCAudioFrame audio;audio.audioFormat=TRTCAudioFrameFormatPCM;audio.data=reinterpret_cast<char*>(inputFrame.data());audio.length=static_cast<uint32_t>(inputFrame.size());audio.sampleRate=rate;audio.channel=1;audio.timestamp=engine->generateCustomPTS();
                engine->sendCustomAudioData(&audio);
                if(playing&&!inputEnded)emit("audio_chunk",{{"last_sample",offset/2},{"enqueued_ms",nowMs()},{"voiced_endpoint_ms",playMs+double(lastVoiceByte)*1000/(rate*2)}});
                if(playing&&!inputEnded&&offset>=pcm.size()){inputEnded=true;emit("pcm_eof",{{"elapsed_from_play_ms",nowMs()-playMs},{"voiced_endpoint_ms",playMs+double(lastVoiceByte)*1000/(rate*2)},{"last_sample",pcm.size()/2},{"silence_continues",true}});}
                if(!image.empty()&&ticks%std::max(1,50/fps)==0){
                    TRTCVideoFrame video;video.videoFormat=TRTCVideoPixelFormat_I420;video.bufferType=TRTCVideoBufferType_Buffer;video.data=reinterpret_cast<char*>(image.data());video.length=static_cast<uint32_t>(image.size());video.width=width;video.height=height;video.timestamp=engine->generateCustomPTS();
                    engine->sendCustomVideoData(TRTCVideoStreamTypeBig,&video);emit("video_frame_submitted",{{"frame_index",videoCount++}});
                }
                std::fill(outputFrame.begin(),outputFrame.end(),0);
                TRTCAudioFrame rendered;rendered.audioFormat=TRTCAudioFrameFormatPCM;rendered.data=reinterpret_cast<char*>(outputFrame.data());rendered.length=static_cast<uint32_t>(outputFrame.size());rendered.sampleRate=48000;rendered.channel=1;
                engine->getCustomAudioRenderingFrame(&rendered);
                if(rendered.length>outputFrame.size())throw std::runtime_error("output audio size exceeds buffer");
                auto levels=level(outputFrame.data(),rendered.length);
                if(output)output.write(reinterpret_cast<const char*>(outputFrame.data()),rendered.length);
                if(playing&&!firstOutput){
                    if(levels.rms>=threshold){if(loudFrames++==0)candidateMs=nowMs();if(loudFrames>=2){firstOutput=true;emit("remote_audio_nonzero",{{"first_window_ms",candidateMs},{"from_voiced_endpoint_ms",candidateMs-playMs-double(lastVoiceByte)*1000/(rate*2)},{"rms_threshold",threshold},{"consecutive_frames",loudFrames}});}}
                    else loudFrames=0;
                }
                emit("output_audio_frame",{{"sample_offset",outputSamples},{"samples",rendered.length/2},{"rms",levels.rms},{"peak",levels.peak}});outputSamples+=rendered.length/2;
            }
            ticks++;next+=std::chrono::milliseconds(20);
            if(Clock::now()>next+std::chrono::milliseconds(20)){emit("pacing_overrun",{{"late_ms",std::chrono::duration<double,std::milli>(Clock::now()-next).count()}});next=Clock::now();}
            std::this_thread::sleep_until(next);
        }
        emit("probe_finished",{{"reason",stopping?"stop_or_stdin_closed":"deadline"},{"audio_file_completed",inputEnded},{"first_output_detected",firstOutput},{"video_frames",videoCount},{"output_samples",outputSamples}});
        engine->exitRoom();auto exitDeadline=Clock::now()+std::chrono::seconds(3);
        while(!handler->exited&&Clock::now()<exitDeadline){pumpMessages();std::this_thread::sleep_for(std::chrono::milliseconds(10));}
        emit("room_exit_confirmation",{{"confirmed",bool(handler->exited)}});
        engine->removeCallback(handler.get());destroyTRTCShareInstance();engine=nullptr;return 0;
    }catch(const std::exception& error){
        bool parser=dynamic_cast<const json::exception*>(&error)!=nullptr;
        emit("probe_error",{{"reason",parser?"invalid JSON configuration or command":error.what()}});
        if(engine){engine->exitRoom();engine->removeCallback(handler.get());destroyTRTCShareInstance();}return 1;
    }
}
