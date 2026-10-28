// Measurement-only native client for the existing Doubao RTC cloud route.
// Uses official Linux Server SDK 3.60.107.700. This is not the production Web
// client: native codec/buffering and Docker networking must be reported separately.
// No microphone, camera, speaker, aircraft SDK, or arbitrary tool execution.
// Config JSON is read from a mounted file; credentials are never emitted.
// stdin JSONL: {"command":"play"}, {"command":"tool_result","id":...,"content":...},
// or {"command":"stop"}. stdout JSONL uses a single steady-clock origin.
#include "bytertc_engine.h"
#include "bytertc_room.h"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <fstream>
#include <iostream>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;
static const auto origin = Clock::now();
static std::mutex outputMutex;
static std::atomic<bool> stopping{false};
static double nowMs() { return std::chrono::duration<double, std::milli>(Clock::now()-origin).count(); }
static void emit(const std::string& event, json fields=json::object()) {
    fields["event"]=event; fields["monotonic_ms"]=nowMs();
    std::lock_guard<std::mutex> lock(outputMutex);
    std::cout << fields.dump(-1, ' ', false, json::error_handler_t::replace) << '\n' << std::flush;
}
static void requireOk(int result, const char* method) {
    if(result<0) throw std::runtime_error(std::string(method)+" returned "+std::to_string(result));
}
static std::vector<uint8_t> readBytes(const std::string& path, size_t maxBytes) {
    std::ifstream in(path, std::ios::binary|std::ios::ate);
    if(!in) throw std::runtime_error("cannot open media file");
    auto size=in.tellg();
    if(size<=0 || static_cast<uint64_t>(size)>maxBytes) throw std::runtime_error("invalid media size");
    std::vector<uint8_t> bytes(static_cast<size_t>(size)); in.seekg(0); in.read(reinterpret_cast<char*>(bytes.data()),size);
    if(!in) throw std::runtime_error("incomplete media file");
    return bytes;
}
static json decodeTLV(const uint8_t* bytes, int size) {
    if(size<8 || size>65536) throw std::runtime_error("invalid TLV size");
    uint32_t length=(uint32_t(bytes[4])<<24)|(uint32_t(bytes[5])<<16)|(uint32_t(bytes[6])<<8)|bytes[7];
    if(length!=static_cast<uint32_t>(size-8)) throw std::runtime_error("invalid TLV length");
    std::string type(reinterpret_cast<const char*>(bytes),4);
    if(!std::all_of(type.begin(),type.end(),[](char c){return c>='a'&&c<='z';})) throw std::runtime_error("invalid TLV type");
    json payload=json::parse(bytes+8,bytes+size);
    if(!payload.is_object()) throw std::runtime_error("invalid TLV payload");
    return {{"type",type},{"payload",payload}};
}
static std::vector<uint8_t> encodeTLV(const std::string& type,const json& payload) {
    auto body=payload.dump(); if(type.size()!=4 || body.size()+8>1000) throw std::runtime_error("RTS message exceeds basic 1KiB budget");
    uint32_t size=body.size(); std::vector<uint8_t> out(type.begin(),type.end());
    out.push_back(size>>24); out.push_back(size>>16); out.push_back(size>>8); out.push_back(size);
    out.insert(out.end(),body.begin(),body.end()); return out;
}
struct Level { double rms; int peak; };
static Level level(const uint8_t* bytes,size_t size) {
    double sum=0; int peak=0; size_t samples=size/2;
    for(size_t i=0;i<samples;i++){int16_t value; std::memcpy(&value,bytes+2*i,2); int a=std::abs(int(value)); peak=std::max(a,peak);sum+=double(value)*value;}
    return {samples?std::sqrt(sum/samples):0,peak};
}

class Handler final : public bytertc::IRTCEngineEventHandler, public bytertc::IRTCRoomEventHandler, public bytertc::IAudioFrameObserver {
public:
    std::string bot;
    std::atomic<bool> joined{false},botReady{false},playing{false},observerFirst{false};
    std::atomic<bytertc::IRTCRoom*> room{nullptr};
    int observerLoud=0;double observerCandidate=0,threshold=64;
    explicit Handler(std::string id):bot(std::move(id)){}
    void onError(int code) override { emit("sdk_error",{{"code",code}}); }
    void onWarning(int code) override { emit("sdk_warning",{{"code",code}}); }
    void onRoomStateChanged(const char*,const char*,int state,const char*) override {
        joined=state==0; emit(state==0?"room_joined":"room_state",{{"state",state}});
        if(state<0) stopping=true;
    }
    void markBotReady(const char* source){if(!botReady.exchange(true))emit("bot_ready",{{"source",source}});}
    void onUserJoined(const bytertc::UserInfo& user) override {
        if(user.uid&&bot==user.uid)markBotReady("user_joined");
    }
    void onUserPublishStreamAudio(const char* stream,const bytertc::StreamInfo& info,bool publish) override {
        if(info.user_id&&bot==info.user_id&&publish){
            markBotReady("audio_published");
            if(auto joinedRoom=room.load())emit("explicit_audio_subscribe",{{"code",joinedRoom->subscribeStreamAudio(stream,true)}});
        }
        emit("remote_audio_publish",{{"published",publish}});
    }
    void onAudioSubscribeStateChanged(const char*,const bytertc::StreamInfo&,bytertc::SubscribeState state,bytertc::SubscribeStateChangeReason reason) override {
        emit("audio_subscribe_state",{{"state",int(state)},{"reason",int(reason)}});
    }
    void onFirstRemoteAudioFrame(const char*,const bytertc::StreamInfo&) override {emit("remote_first_decoded_frame");}
    void onRemoteAudioStateChanged(const char*,const bytertc::StreamInfo&,bytertc::RemoteAudioState state,bytertc::RemoteAudioStateChangeReason reason) override {
        emit("remote_audio_state",{{"state",int(state)},{"reason",int(reason)}});
    }
    void onRecordAudioFrameOriginal(const bytertc::IAudioFrame&) override{}
    void onRecordAudioFrame(const bytertc::IAudioFrame&) override{}
    void onPlaybackAudioFrame(const bytertc::IAudioFrame&) override{}
    void onMixedAudioFrame(const bytertc::IAudioFrame&) override{}
    void onCaptureMixedAudioFrame(const bytertc::IAudioFrame&) override{}
    void onRemoteUserAudioFrame(const char*,const bytertc::StreamInfo& info,const bytertc::IAudioFrame& frame) override {
        if(!info.user_id||bot!=info.user_id)return;
        auto levels=level(frame.data(),frame.dataSize());
        emit("remote_audio_observer_frame",{{"rms",levels.rms},{"peak",levels.peak},{"samples",frame.dataSize()/2},{"sample_rate",int(frame.sampleRate())},{"channels",int(frame.channel())}});
        if(playing&&!observerFirst){
            if(levels.rms>=threshold){if(observerLoud++==0)observerCandidate=nowMs();if(observerLoud>=2){observerFirst=true;emit("remote_audio_nonzero",{{"first_window_ms",observerCandidate},{"source","remote_user_observer"},{"rms_threshold",threshold},{"consecutive_frames",observerLoud}});}}
            else observerLoud=0;
        }
    }
    void received(const char* uid,int size,const uint8_t* bytes) {
        if(!uid || bot!=uid){emit("ignored_sender");return;}
        try{
            auto decoded=decodeTLV(bytes,size);markBotReady("authenticated_tlv");
            emit("tlv",decoded);
            if(decoded["type"]=="conv"&&decoded["payload"].contains("Stage")&&decoded["payload"]["Stage"].value("Code",0)==1)emit("bot_listening");
        }catch(const std::exception&){emit("malformed_tlv",{{"bytes",size}});}
    }
    void onUserBinaryMessageReceived(const char* uid,int size,const uint8_t* bytes) override {received(uid,size,bytes);}
    void onRoomBinaryMessageReceived(const char* uid,int size,const uint8_t* bytes) override {received(uid,size,bytes);}
    void onUserBinaryMessageReceived(int64_t,const char* uid,const uint8_t* bytes,int size) override {received(uid,size,bytes);}
    void onRoomBinaryMessageReceived(int64_t,const char* uid,const uint8_t* bytes,int size) override {received(uid,size,bytes);}
    void onUserMessageSendResult(int64_t id,int error) override {emit("message_send_result",{{"message_id",id},{"error",error}});}
};

static int selfTest() {
    const json body={{"ToolCallID","abc"},{"Content","{}"}};
    auto bytes=encodeTLV("func",body); auto parsed=decodeTLV(bytes.data(),bytes.size());
    if(parsed["payload"]!=body) throw std::runtime_error("TLV round trip failed");
    bytes[7]++;
    try{decodeTLV(bytes.data(),bytes.size());throw std::logic_error("invalid length accepted");}catch(const std::runtime_error&){}
    const int16_t samples[]={0,-1000,1000,0};auto measured=level(reinterpret_cast<const uint8_t*>(samples),sizeof(samples));
    if(measured.peak!=1000 || std::abs(measured.rms-std::sqrt(500000.0))>0.001) throw std::runtime_error("PCM level calculation failed");
    emit("self_test",{{"ok",true},{"network",false},{"devices",false}}); return 0;
}

int main(int argc,char** argv) {
    std::signal(SIGTERM,[](int){stopping=true;}); std::signal(SIGINT,[](int){stopping=true;});
    if(argc==2&&std::string(argv[1])=="--self-test")return selfTest();
    bytertc::IRTCEngine* engine=nullptr; bytertc::IRTCRoom* room=nullptr;
    // Handler outlives engine even on exceptional cleanup.
    std::unique_ptr<Handler> handler;
    try{
        if(argc!=2)throw std::runtime_error("usage: NativeProbe /mounted/config.json");
        std::ifstream configIn(argv[1]); if(!configIn)throw std::runtime_error("cannot read config");
        json config;configIn>>config;
        const std::string appId=config.at("appId"),token=config.at("token"),roomId=config.at("roomId"),userId=config.at("userId"),botId=config.at("botId");
        int rate=config.value("sampleRate",16000),width=config.value("width",640),height=config.value("height",360),fps=config.value("fps",2);
        int maxSeconds=config.value("maxSeconds",45); double threshold=config.value("rmsThreshold",64.0);
        if((rate!=16000&&rate!=48000)||width<2||height<2||width>1920||height>1080||width%2||height%2||fps<1||fps>30||maxSeconds<1||maxSeconds>180||threshold<1||threshold>32000)
            throw std::runtime_error("invalid probe parameters");
        auto pcm=readBytes(config.at("pcmPath"),rate*2*30);if(pcm.size()%2)throw std::runtime_error("PCM must be mono S16LE");
        auto image=readBytes(config.at("imagePath"),width*height*3/2);
        if(image.size()!=size_t(width*height*3/2))throw std::runtime_error("I420 size mismatch");
        std::ofstream output;
        if(config.contains("outputPcmPath")){output.open(config["outputPcmPath"].get<std::string>(),std::ios::binary);if(!output)throw std::runtime_error("cannot write output PCM");}
        emit("probe_start",{{"route","DoubaoRTC"},{"client","official_linux_server_native"},{"sdk_version","3.60.107.700"},{"production_client","Web 4.69.3"},{"audio_rate",rate},{"audio_bytes",pcm.size()},{"image_width",width},{"image_height",height},{"image_fps",fps},{"hardware",false},{"audio_sink","external_pull_48k_mono_s16le"}});
        handler=std::make_unique<Handler>(botId);handler->threshold=threshold;
        bytertc::EngineConfig ec;ec.app_id=appId.c_str();
        engine=bytertc::IRTCEngine::createRTCEngine(ec,handler.get());if(!engine)throw std::runtime_error("cannot create RTC engine");
        requireOk(engine->setAudioSourceType(bytertc::kAudioSourceTypeExternal),"setAudioSourceType");
        requireOk(engine->setAudioRenderType(bytertc::kAudioRenderTypeExternal),"setAudioRenderType");
        requireOk(engine->registerAudioFrameObserver(handler.get()),"registerAudioFrameObserver");
        bytertc::AudioFormat observerFormat;
        requireOk(engine->enableAudioFrameCallback(bytertc::AudioFrameCallbackMethod::kRemoteUser,observerFormat),"enableAudioFrameCallback");
        requireOk(engine->setVideoSourceType(bytertc::kVideoSourceTypeExternal),"setVideoSourceType");
        bytertc::VideoEncoderConfig videoConfig;videoConfig.width=width;videoConfig.height=height;videoConfig.frame_rate=fps;
        requireOk(engine->setVideoEncoderConfig(videoConfig),"setVideoEncoderConfig");
        room=engine->createRTCRoom(roomId.c_str());if(!room)throw std::runtime_error("cannot create room");handler->room=room;
        requireOk(room->setRTCRoomEventHandler(handler.get()),"setRTCRoomEventHandler");
        bytertc::UserInfo user;user.uid=userId.c_str();
        bytertc::RTCRoomConfig rc;rc.room_profile_type=bytertc::kRoomProfileTypeCommunication;
        rc.is_auto_publish_audio=true;rc.is_auto_publish_video=true;rc.is_auto_subscribe_audio=true;rc.is_auto_subscribe_video=false;
        emit("join_requested");requireOk(room->joinRoom(token.c_str(),user,true,rc),"joinRoom");
        size_t audioOffset=0; uint64_t pushedSamples=0,outputSamples=0,videoCount=0,ticks=0;
        bool playing=false,inputEnded=false,firstOutput=false;double playMs=0;int loudFrames=0;
        double candidateMs=0;size_t lastVoiceByte=0;
        // Fixed offline input endpoint, independent of the provider VAD.
        for(size_t i=0;i+1<pcm.size();i+=2){int16_t value;std::memcpy(&value,pcm.data()+i,2);if(std::abs(int(value))>=threshold)lastVoiceByte=i+2;}
        if(config.contains("speechEndSamples")){
            auto speechSamples=config["speechEndSamples"].get<size_t>();
            if(!speechSamples||speechSamples>pcm.size()/2)throw std::runtime_error("invalid speech endpoint");
            lastVoiceByte=speechSamples*2;
        }
        std::vector<uint8_t> inputFrame(rate/100*2),outputFrame(480*2);
        const auto deadline=Clock::now()+std::chrono::seconds(maxSeconds);auto next=Clock::now();std::string inputLines;
        while(!stopping&&Clock::now()<deadline){
            pollfd fd{STDIN_FILENO,POLLIN,0};
            if(poll(&fd,1,0)>0&&(fd.revents&(POLLIN|POLLHUP))){
                char buffer[4096];auto count=read(STDIN_FILENO,buffer,sizeof(buffer));
                if(count<=0){stopping=true;break;}inputLines.append(buffer,count);
                if(inputLines.size()>65536)throw std::runtime_error("stdin command too large");
                size_t newline;
                while((newline=inputLines.find('\n'))!=std::string::npos){
                    auto line=inputLines.substr(0,newline);inputLines.erase(0,newline+1);if(line.empty())continue;
                    json command=json::parse(line);std::string kind=command.at("command");
                    if(kind=="stop"){stopping=true;break;}
                    if(kind=="play"){
                        if(playing||!handler->joined||!handler->botReady)throw std::runtime_error("play requires joined bot, once per session");
                        playing=true;handler->playing=true;playMs=nowMs();emit("audio_input_started",{{"speech_endpoint_offset_ms",double(lastVoiceByte)*1000/(rate*2)},{"pcm_duration_ms",double(pcm.size())*1000/(rate*2)}});
                    }else if(kind=="tool_result"){
                        std::string id=command.at("id"),content=command.at("content");
                        auto bytes=encodeTLV("func",{{"ToolCallID",id},{"Content",content}});
                        emit("tool_result_send_started",{{"id",id}});
                        auto message=room->sendUserBinaryMessage(botId.c_str(),bytes.size(),bytes.data());
                        emit("tool_result_submitted",{{"id",id},{"message_id",message}});
                    }else if(kind!="play")throw std::runtime_error("unknown stdin command");
                }
            }
            if(stopping)break;
            if(handler->joined){
                std::fill(inputFrame.begin(),inputFrame.end(),0);
                if(playing&&!inputEnded){
                    size_t count=std::min(inputFrame.size(),pcm.size()-audioOffset);
                    std::copy_n(pcm.data()+audioOffset,count,inputFrame.data());audioOffset+=count;
                }
                bytertc::AudioFrameBuilder ab{};ab.sample_rate=static_cast<bytertc::AudioSampleRate>(rate);ab.channel=bytertc::kAudioChannelMono;
                ab.data=inputFrame.data();ab.data_size=inputFrame.size();ab.timestamp_us=pushedSamples*1000000/rate;
                auto audio=bytertc::buildAudioFrame(ab);if(!audio)throw std::runtime_error("build input frame failed");
                int result=engine->pushExternalAudioFrame(audio);audio->release();requireOk(result,"pushExternalAudioFrame");pushedSamples+=rate/100;
                // Native API requires 10ms pushes; the common 20ms PCM chunks
                // are split into two SDK calls, with a 20ms accounting event.
                if(playing&&!inputEnded)emit("audio_chunk",{{"last_sample",audioOffset/2},{"enqueued_ms",nowMs()},{"voiced_endpoint_ms",playMs+double(lastVoiceByte)*1000/(rate*2)}});
                if(playing&&!inputEnded&&audioOffset>=pcm.size()){
                    inputEnded=true;emit("pcm_eof",{{"elapsed_from_play_ms",nowMs()-playMs},{"voiced_endpoint_ms",playMs+double(lastVoiceByte)*1000/(rate*2)},{"last_sample",pcm.size()/2},{"silence_continues",true}});
                }
                if(ticks%std::max(1,100/fps)==0){
                    bytertc::VideoFrameData vf;vf.pixel_format=bytertc::kVideoPixelFormatI420;vf.number_of_planes=3;vf.width=width;vf.height=height;
                    vf.plane_data[0]=image.data();vf.plane_data[1]=image.data()+width*height;vf.plane_data[2]=image.data()+width*height*5/4;
                    vf.plane_stride[0]=width;vf.plane_stride[1]=width/2;vf.plane_stride[2]=width/2;
                    vf.timestamp_us=static_cast<int64_t>(nowMs()*1000);
                    requireOk(engine->pushExternalVideoFrame(vf),"pushExternalVideoFrame");
                    emit("video_frame_submitted",{{"frame_index",videoCount++}});
                }
                bytertc::AudioFrameBuilder ob{};ob.sample_rate=bytertc::kAudioSampleRate48000;ob.channel=bytertc::kAudioChannelMono;ob.data=outputFrame.data();ob.data_size=outputFrame.size();
                auto rendered=bytertc::buildAudioFrame(ob);if(!rendered)throw std::runtime_error("build output frame failed");
                result=engine->pullExternalAudioFrame(rendered);
                if(result==0){
                    const auto* data=rendered->data();int size=rendered->dataSize();auto levels=level(data,size);
                    if(output)output.write(reinterpret_cast<const char*>(data),size);
                    if(playing&&!firstOutput){
                        if(levels.rms>=threshold){if(loudFrames++==0)candidateMs=nowMs();if(loudFrames>=2){firstOutput=true;emit("remote_audio_nonzero",{{"first_window_ms",candidateMs},{"from_voiced_endpoint_ms",candidateMs-playMs-double(lastVoiceByte)*1000/(rate*2)},{"rms_threshold",threshold},{"consecutive_frames",loudFrames}});}}
                        else loudFrames=0;
                    }
                    emit("output_audio_frame",{{"sample_offset",outputSamples},{"samples",size/2},{"rms",levels.rms},{"peak",levels.peak}});outputSamples+=size/2;
                }else if(ticks%100==0)emit("output_pull_status",{{"code",result}});
                rendered->release();
            }
            ++ticks;next+=std::chrono::milliseconds(10);
            if(Clock::now()>next+std::chrono::milliseconds(20)){emit("pacing_overrun",{{"late_ms",std::chrono::duration<double,std::milli>(Clock::now()-next).count()}});next=Clock::now();}
            std::this_thread::sleep_until(next);
        }
        emit("probe_finished",{{"reason",stopping?"stop_or_stdin_closed":"deadline"},{"audio_file_completed",inputEnded},{"first_output_detected",firstOutput},{"video_frames",videoCount},{"output_samples",outputSamples}});
        handler->room=nullptr;room->leaveRoom();room->destroy();room=nullptr;bytertc::IRTCEngine::destroyRTCEngine();engine=nullptr;
        return 0;
    }catch(const std::exception& error){
        // Parser diagnostics can echo input text, including a short-lived token.
        const bool jsonError=dynamic_cast<const json::exception*>(&error)!=nullptr;
        emit("probe_error",{{"reason",jsonError?"invalid JSON configuration or command":error.what()}});
        if(room){room->leaveRoom();room->destroy();}if(engine)bytertc::IRTCEngine::destroyRTCEngine();return 1;
    }
}
