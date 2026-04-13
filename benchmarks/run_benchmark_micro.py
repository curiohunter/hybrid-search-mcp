"""Micro benchmark — embed only query+golden pairs, no full index needed.

Tests: 30 queries × 30 golden code snippets = 60 texts total.
Measures cosine similarity between Korean/English query and its expected code.
Total inference: ~60 texts per model. Should finish in <2 min per model.
"""

from __future__ import annotations

import json
import gc
import sys
import time
from pathlib import Path

import numpy as np

# Golden pairs: query → code snippet (manually extracted from valuein_homepage)
GOLDEN_PAIRS = [
    # Korean NL → English code
    ("로그인 처리", "async function login(formData: FormData) { const email = formData.get('email'); const { error } = await supabase.auth.signInWithPassword({ email, password }); }"),
    ("학원비 납부 현황", "export async function getTuitionFees(month: string): Promise<TuitionFee[]> { const { data } = await supabase.from('tuition_fees').select('*').eq('month', month); }"),
    ("학생 출석 체크", "export async function checkIn(studentId: string, classId: string): Promise<Attendance> { return updateAttendance({ student_id: studentId, status: 'present', check_in_time: new Date() }); }"),
    ("환불 계산", "export async function calculateWithdrawal(studentId: string): Promise<WithdrawalCalculation> { const tuitionFees = await getUnpaidTuitionFees(studentId); const refundAmount = calculateRefundAmount(tuitionFees, withdrawalDate); }"),
    ("상담 기록 분석", "export async function analyzeConsultation(consultationId: string): Promise<ConsultationAnalysis> { const consultation = await getConsultation(consultationId); const analysis = await anthropic.messages.create({ model: 'claude', messages: buildPrompt(consultation) }); }"),
    ("수업 일정 관리", "export async function createEvent(event: CalendarEvent): Promise<CalendarEvent> { const { data } = await supabase.from('calendar_events').insert({ title: event.title, start_time: event.startTime, recurrence_rule: event.recurrenceRule }); }"),
    ("교재 배정하기", "export async function assignTextbook(studentId: string, textbookId: string): Promise<TextbookAssignment> { const { data } = await supabase.from('textbook_assignments').insert({ student_id: studentId, textbook_id: textbookId }); }"),
    ("직원 목록 조회", "export async function getEmployees(): Promise<Employee[]> { const { data } = await supabase.from('employees').select('*, roles(*)').order('name'); }"),
    ("비대면 수업 개설", "export async function createRemoteRoom(teacherId: string, classId: string): Promise<RemoteRoom> { const room = await supabase.from('remote_rooms').insert({ teacher_id: teacherId, class_id: classId, status: 'open' }); }"),
    ("학생 성적 채점", "export async function upsertGradingResult(input: GradingInput): Promise<GradingResult> { const { data } = await supabase.from('grading_results').upsert({ student_id: input.studentId, section_id: input.sectionId, score: input.score }); }"),
    # English NL → English code
    ("authentication callback handler", "export async function GET(request: NextRequest) { const code = searchParams.get('code'); const { data } = await supabase.auth.exchangeCodeForSession(code); redirect('/dashboard'); }"),
    ("generate monthly tuition fees", "export async function generateMonthlyTuition(month: string, classId: string): Promise<TuitionFee[]> { const students = await getStudentsByClass(classId); const fees = students.map(s => ({ student_id: s.id, amount: s.class_fee, month, status: 'unpaid' })); }"),
    ("bulk attendance check in", "export async function bulkCheckIn(inputs: BulkCheckInInput[]): Promise<Attendance[]> { const results = await Promise.all(inputs.map(input => checkIn(input.studentId, input.classId))); }"),
    ("send payment invoice", "export async function sendInvoice(studentId: string, tuitionFeeId: string): Promise<PaysSamResponse> { const bill = await getActiveBill(tuitionFeeId); const response = await paysSamClient.post('/invoices', { amount: bill.amount, recipient: student.phone }); }"),
    ("student credit balance", "export async function getCreditBalance(studentId: string): Promise<number> { const credits = await getActiveCredits(studentId); return credits.reduce((sum, c) => sum + c.remaining_amount, 0); }"),
    ("recurring calendar events expansion", "export function expandAllEvents(events: CalendarEvent[], startDate: Date, endDate: Date): FullCalendarEvent[] { return events.flatMap(event => event.recurrenceRule ? expandRecurrence(event, startDate, endDate) : [toFullCalendarEvent(event)]); }"),
    ("create student record", "export async function createStudent(input: CreateStudentInput): Promise<Student> { const { data } = await supabase.from('students').insert({ name: input.name, phone: input.phone, department: input.department, status: 'active' }); }"),
    ("AI generated learning comments", "export async function generateComment(studentId: string, period: string): Promise<string> { const logs = await getLearningLogs(studentId, period); const prompt = buildPrompt(logs); const response = await anthropic.messages.create({ model: 'claude', messages: [{ role: 'user', content: prompt }] }); }"),
    ("dashboard statistics and metrics", "export async function getDashboardStats(): Promise<DashboardMetric[]> { const [students, tuition, attendance] = await Promise.all([getStudentCount(), getTuitionSummary(), getAttendanceRate()]); }"),
    ("delete user account", "export async function DELETE(request: NextRequest) { const userId = await getAuthUserId(); await supabase.auth.admin.deleteUser(userId); await supabase.from('employees').delete().eq('auth_id', userId); }"),
    # Mixed (Korean + English symbols)
    ("checkIn 함수가 뭐하는지", "export async function checkIn(studentId: string, classId: string): Promise<Attendance> { const attendance = await updateAttendance({ student_id: studentId, status: 'present', check_in_time: new Date() }); }"),
    ("tuition 월별 생성 로직", "export async function generateMonthlyTuition(month: string): Promise<TuitionFee[]> { const classes = await getClassesWithStudents(); for (const cls of classes) { const fees = cls.students.map(s => createTuitionFee(s.id, cls.fee, month)); } }"),
    ("PaysSam webhook 처리", "export async function handlePaysSamWebhook(payload: WebhookPayload): Promise<void> { const { bill_id, status } = payload; await supabase.from('tuition_fees').update({ payment_status: status }).eq('payssam_bill_id', bill_id); }"),
    ("student withdrawal 환불 프로세스", "export async function completeWithdrawal(processId: string): Promise<void> { const process = await getWithdrawalProcess(processId); await sendSettlementInvoice(process); await removeStudentFromClasses(process.studentId); await softDeleteStudent(process.studentId); }"),
    ("RemoteRoom 도움 요청 기능", "export async function requestHelp(roomId: string, studentId: string, message: string): Promise<RemoteRoomHelp> { const help = await supabase.from('remote_room_helps').insert({ room_id: roomId, student_id: studentId, message, status: 'pending' }); }"),
    ("Meeting actionItem 상태 관리", "export async function createActionItem(meetingId: string, input: ActionItemInput): Promise<MeetingActionItem> { const { data } = await supabase.from('meeting_action_items').insert({ meeting_id: meetingId, title: input.title, assignee_id: input.assigneeId, status: 'pending' }); }"),
    ("consultation 이탈 위험도 분석", "export async function analyzeConsultation(consultationId: string): Promise<{ churnRisk: ConsultationChurnRisk }> { const consultation = await getConsultation(consultationId); const analysis = await anthropic.messages.create({ messages: buildChurnAnalysisPrompt(consultation) }); }"),
    ("Supabase 서버 클라이언트 생성", "export function createServerClient() { const cookieStore = cookies(); return createServerComponentClient<Database>({ cookies: () => cookieStore }); }"),
    ("textbook grading 섹션별 문제", "export async function getProblemsBySection(sectionId: string): Promise<Problem[]> { const { data } = await supabase.from('problems').select('*').eq('section_id', sectionId).order('order_num'); }"),
    ("admission 입시 결과 등록", "export async function createAdmission(input: CreateAdmissionInput): Promise<AdmissionResult> { const { data } = await supabase.from('admission_results').insert({ student_id: input.studentId, university: input.university, admission_type: input.admissionType, result: input.result }); }"),
]

CATEGORIES = ["korean_nl"] * 10 + ["english_nl"] * 10 + ["mixed"] * 10

MODELS = [
    {"name": "e5-small", "hf_id": "intfloat/multilingual-e5-small"},
    {"name": "e5-base", "hf_id": "intfloat/multilingual-e5-base"},
]

MODELS_TRY = [
    {"name": "Qwen3-Emb-0.6B", "hf_id": "Qwen/Qwen3-Embedding-0.6B"},
]


def test_model(model_info: dict) -> dict:
    from sentence_transformers import SentenceTransformer

    print(f"  Loading {model_info['hf_id']}...")
    model = SentenceTransformer(model_info["hf_id"], trust_remote_code=True)

    queries = [f"query: {p[0]}" for p in GOLDEN_PAIRS]
    passages = [f"passage: {p[1]}" for p in GOLDEN_PAIRS]

    t0 = time.monotonic()
    q_embs = model.encode(queries, normalize_embeddings=True, batch_size=16)
    p_embs = model.encode(passages, normalize_embeddings=True, batch_size=16)
    elapsed = time.monotonic() - t0

    # Cosine similarity for each pair
    sims = [float(np.dot(q_embs[i], p_embs[i])) for i in range(len(GOLDEN_PAIRS))]

    # Cross-similarity: each query vs ALL passages (simulates retrieval)
    sim_matrix = q_embs @ p_embs.T  # (30, 30)

    recall_at_1, recall_at_3, recall_at_5 = [], [], []
    mrrs = []
    by_cat: dict[str, list] = {}

    for i in range(len(GOLDEN_PAIRS)):
        ranks = np.argsort(-sim_matrix[i])  # descending
        true_rank = int(np.where(ranks == i)[0][0]) + 1  # 1-indexed

        r1 = 1.0 if true_rank <= 1 else 0.0
        r3 = 1.0 if true_rank <= 3 else 0.0
        r5 = 1.0 if true_rank <= 5 else 0.0
        rr = 1.0 / true_rank

        recall_at_1.append(r1)
        recall_at_3.append(r3)
        recall_at_5.append(r5)
        mrrs.append(rr)

        cat = CATEGORIES[i]
        by_cat.setdefault(cat, []).append({"r1": r1, "r3": r3, "r5": r5, "rr": rr, "sim": sims[i], "rank": true_rank})

    result = {
        "model": model_info["name"],
        "recall@1": round(np.mean(recall_at_1), 4),
        "recall@3": round(np.mean(recall_at_3), 4),
        "recall@5": round(np.mean(recall_at_5), 4),
        "mrr": round(np.mean(mrrs), 4),
        "avg_sim": round(np.mean(sims), 4),
        "embed_time_s": round(elapsed, 1),
        "by_category": {},
        "per_query": [],
    }

    for cat, vals in by_cat.items():
        result["by_category"][cat] = {
            "recall@1": round(np.mean([v["r1"] for v in vals]), 4),
            "recall@3": round(np.mean([v["r3"] for v in vals]), 4),
            "recall@5": round(np.mean([v["r5"] for v in vals]), 4),
            "mrr": round(np.mean([v["rr"] for v in vals]), 4),
            "avg_sim": round(np.mean([v["sim"] for v in vals]), 4),
        }

    for i, (q, p) in enumerate(GOLDEN_PAIRS):
        ranks = np.argsort(-sim_matrix[i])
        true_rank = int(np.where(ranks == i)[0][0]) + 1
        result["per_query"].append({
            "query": q,
            "category": CATEGORIES[i],
            "sim": round(sims[i], 4),
            "rank": true_rank,
        })

    del model
    gc.collect()
    return result


def main():
    print("=" * 60)
    print("Micro Benchmark — Query-Golden Pair Similarity")
    print(f"30 pairs, {len(MODELS) + len(MODELS_TRY)} models")
    print("=" * 60)

    results = []
    for m in MODELS + MODELS_TRY:
        print(f"\n--- {m['name']} ---")
        try:
            r = test_model(m)
            results.append(r)
            print(f"  R@1={r['recall@1']}  R@3={r['recall@3']}  R@5={r['recall@5']}  MRR={r['mrr']}  AvgSim={r['avg_sim']}  ({r['embed_time_s']}s)")
            for cat, v in r["by_category"].items():
                print(f"    {cat}: R@1={v['recall@1']} R@3={v['recall@3']} MRR={v['mrr']} Sim={v['avg_sim']}")
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"model": m["name"], "error": str(e)})

    # Summary
    print("\n" + "=" * 60)
    print(f"{'Model':<18} {'R@1':>5} {'R@3':>5} {'R@5':>5} {'MRR':>6} {'Sim':>6} {'KR_R@1':>7} {'EN_R@1':>7} {'MX_R@1':>7} {'sec':>5}")
    print("-" * 80)
    for r in results:
        if "error" in r:
            print(f"{r['model']:<18} ERROR: {r['error'][:50]}")
            continue
        kr = r["by_category"].get("korean_nl", {})
        en = r["by_category"].get("english_nl", {})
        mx = r["by_category"].get("mixed", {})
        print(
            f"{r['model']:<18} "
            f"{r['recall@1']:>5.2f} {r['recall@3']:>5.2f} {r['recall@5']:>5.2f} "
            f"{r['mrr']:>6.3f} {r['avg_sim']:>6.3f} "
            f"{kr.get('recall@1',0):>7.2f} {en.get('recall@1',0):>7.2f} {mx.get('recall@1',0):>7.2f} "
            f"{r['embed_time_s']:>5.0f}"
        )

    # Show misses (rank > 5)
    for r in results:
        if "error" in r:
            continue
        misses = [q for q in r["per_query"] if q["rank"] > 5]
        if misses:
            print(f"\n{r['model']} — {len(misses)} queries rank>5:")
            for q in sorted(misses, key=lambda x: -x["rank"]):
                print(f"  rank={q['rank']:>2} sim={q['sim']:.3f} [{q['category']}] \"{q['query']}\"")

    # Save
    out = Path(__file__).parent / "benchmark_micro_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
