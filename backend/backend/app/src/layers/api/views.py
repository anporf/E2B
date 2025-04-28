import base64
import builtins
import copy
from datetime import datetime
from extensions import utils
import json
from http import HTTPStatus
import os
import decimal
import typing as t
import traceback
import uuid
import enum
from uuid import UUID


from django import http
from django.contrib.auth.models import User
from django.shortcuts import render
from django.utils import timezone as djtz
from django.views import View
from django.conf import settings
from lxml import etree
import xmltodict

from app.src.connectors.api_domain.model_converters import DomainToApiModelConverter
from app.src.connectors.domain_storage.model_converters import StorageToDomainModelConverter
from app.src.exceptions import UserError
from app.src.layers.api.models import ApiModel, meddra, code_set
from app.src.layers.api.models.logging import Log
from app.src.layers.base.services import (
    BusinessServiceProtocol, 
    CIOMSServiceProtocol, 
    CodeSetServiceProtocol,
    MedDRAServiceProtocol
)
from app.src.enums import NullFlavor as NF
import app.src.enums as enums
from app.src.layers.api.models.icsr import (
    ICSR, C_1_identification_case_safety_report, C_1_6_1_r_documents_held_sender,
    C_1_9_1_r_source_case_id, C_1_10_r_identification_number_report_linked,
    C_2_r_primary_source_information, C_3_information_sender_case_safety_report,
    C_4_r_literature_reference, C_5_study_identification, C_5_1_r_study_registration,
    D_patient_characteristics, D_7_1_r_structured_information_medical_history,
    D_8_r_past_drug_history, D_9_2_r_cause_death, D_9_4_r_autopsy_determined_cause_death,
    D_10_7_1_r_structured_information_parent_meddra_code, D_10_8_r_past_drug_history_parent,
    E_i_reaction_event, F_r_results_tests_procedures_investigation_patient,
    G_k_drug_information, G_k_2_3_r_substance_id_strength, G_k_4_r_dosage_information,
    G_k_7_r_indication_use_case, G_k_9_i_drug_reaction_matrix,
    G_k_9_i_2_r_assessment_relatedness_drug_reaction, G_k_10_r_additional_information_drug,
    H_narrative_case_summary, H_3_r_sender_diagnosis_meddra_code,
    H_5_r_case_summary_reporter_comments_native_language
)


def log(method: t.Callable[[http.HttpRequest], http.HttpResponse]) \
-> t.Callable[[http.HttpRequest], http.HttpResponse]:
    
    def wrapper(self: View, request: http.HttpRequest, *args, **kwargs) -> http.HttpResponse:
        log = Log.create_from_request(request)

        exc = None
        try:
            response = method(self, request, *args, **kwargs)
            log.status = response.status_code
        except Exception as e:
            exc = e
            log.status = 500

        log.response_time = djtz.now()
        log.save()

        if exc:
            raise exc
        return response
    
    return wrapper


class AuthView(View):
    def dispatch(self, request: http.HttpRequest, *args, **kwargs) -> http.HttpResponse:
        print("DEBUG: Request headers:")
        for header, value in request.META.items():
            if header.startswith('HTTP_'):
                print(f"DEBUG:   {header}: {value}")
        try:
            auth_header = request.META['HTTP_AUTHORIZATION']
            print(f"DEBUG: Found authorization header: {auth_header}")
            encoded_credentials = auth_header.split(' ')[1]  # Remove "Basic " to isolate credentials
            decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8').split(':')
            username = decoded_credentials[0]
            password = decoded_credentials[1]
            print(f"DEBUG: Extracted username: {username}")
        except Exception as e:
            print(f"DEBUG: Authorization error: {str(e)}")
            return http.HttpResponse('Invalid HTTP_AUTHORIZATION header', status=HTTPStatus.UNAUTHORIZED)
        
        try:
            user = User.objects.get(username=username)
            is_valid = user.check_password(password)
            print(f"DEBUG: User found, password valid: {is_valid}")
        except User.DoesNotExist:
            print(f"DEBUG: User not found: {username}")
            is_valid = False

        if not is_valid:
            return http.HttpResponse('Invalid username or password', status=HTTPStatus.UNAUTHORIZED)
        
        request.user = user
        return super().dispatch(request, *args, **kwargs)


class AuthCheckView(AuthView):    
    def get(self, request: http.HttpRequest) -> http.HttpResponse:
        response = http.HttpResponse(
            json.dumps({"authenticated": True, "username": request.user.username}), 
            content_type="application/json"
        )
        response["Access-Control-Allow-Origin"] = "*"  # Или конкретный источник
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Origin, Content-Type, Accept, Authorization"
        return response
    
    def options(self, request: http.HttpRequest) -> http.HttpResponse:
        # Обработка preflight запроса
        response = http.HttpResponse()
        response["Access-Control-Allow-Origin"] = "*"  # Или конкретный источник
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Origin, Content-Type, Accept, Authorization"
        return response

class BaseView(AuthView):
    domain_service: BusinessServiceProtocol[ApiModel] = ...
    model_class: type[ApiModel] = ...

    def dispatch(self, request: http.HttpRequest, *args, **kwargs) -> http.HttpResponse:
        try:
            return super().dispatch(request, *args, **kwargs)
        except (TypeError, json.JSONDecodeError):
            return http.HttpResponse('Invalid json data', status=HTTPStatus.BAD_REQUEST)
        except UserError as e:
            return http.HttpResponse(str(e), status=HTTPStatus.BAD_REQUEST)

    def get_model_from_request(self, request: http.HttpRequest) -> ApiModel:
        data = json.loads(request.body)
        model = self.model_class.model_dict_construct(data)
        return model.model_safe_validate(data)

    def get_status_code(self, is_ok: bool) -> HTTPStatus:
        return HTTPStatus.OK if is_ok else HTTPStatus.BAD_REQUEST

    def respond_with_model_as_json(self, model: ApiModel, status: HTTPStatus) -> http.HttpResponse:
        # Dump data and ignore warnings about wrong data format and etc.
        data = utils.exec_without_warnings(lambda: model.model_dump_json(by_alias=True))
        return self.respond_with_json(data, status)

    def respond_with_object_as_json(self, obj: t.Any, status: HTTPStatus) -> http.HttpResponse:
        return self.respond_with_json(json.dumps(obj), status)

    def respond_with_json(self, json_str: str, status: HTTPStatus) -> http.HttpResponse:
        return http.HttpResponse(json_str, status=status, content_type='application/json')


class ModelClassView(BaseView):
    def get(self, request: http.HttpRequest) -> http.HttpResponse:
        result_list = self.domain_service.list(self.model_class)
        return self.respond_with_object_as_json(result_list, HTTPStatus.OK)

    @log
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        model = self.get_model_from_request(request)
        if model.is_valid:
            # TODO: check id empty
            model, is_ok = self.domain_service.create(model)
        else:
            is_ok = False
        status = self.get_status_code(is_ok)
        return self.respond_with_model_as_json(model, status)


class ModelInstanceView(BaseView):
    def get(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        model = self.domain_service.read(self.model_class, pk)
        return self.respond_with_model_as_json(model, HTTPStatus.OK)

    @log
    def put(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        # TODO: check pk = model.id
        model = self.get_model_from_request(request)
        if model.is_valid:
            model, is_ok = self.domain_service.update(model, pk)
        else:
            is_ok = False
        status = self.get_status_code(is_ok)
        return self.respond_with_model_as_json(model, status)

    @log
    def delete(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        is_ok = self.domain_service.delete(self.model_class, pk)
        status = self.get_status_code(is_ok)
        return http.HttpResponse(status=status)


class ModelBusinessValidationView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        model = self.get_model_from_request(request)
        if model.is_valid:
            model, is_ok = self.domain_service.business_validate(model)
        else:
            is_ok = False
        status = self.get_status_code(is_ok)
        return self.respond_with_model_as_json(model, status)


class ModelToXmlView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        model = self.get_model_from_request(request)
        model_dict = model.model_dump()
        self.extend_lists(model_dict)
        model_dict = {self.model_class.__name__: model_dict}
        result = xmltodict.unparse(model_dict)
        return http.HttpResponse(result, content_type='application/xml')

    # Is needed to fix issue with single item list in xmltodict lib
    @classmethod
    def extend_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for value in model_dict.values():
            if isinstance(value, dict):
                cls.extend_lists(value)
            if isinstance(value, list) and len(value) == 1:
                value.append(dict())


class ModelFromXmlView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        xml = json.loads(request.body)['value']
        model_dict = xmltodict.parse(xml)
        model_dict = model_dict[self.model_class.__name__]
        self.reduce_lists(model_dict)
        model = self.model_class(**model_dict)
        return self.respond_with_model_as_json(model, HTTPStatus.OK)

    # Is needed to fix issue with single item list in xmltodict lib
    @classmethod
    def reduce_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for value in model_dict.values():
            if isinstance(value, dict):
                cls.reduce_lists(value)
            if isinstance(value, list) and len(value) == 2 and value[1] is None:
                value.pop(1)


class ModelCIOMSView(View):
    cioms_service: CIOMSServiceProtocol = ...

    def get(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        cioms_data = self.cioms_service.convert_icsr_to_cioms(pk)
        
        for field in ['f7_13_describe_reactions', 'f22_concomitant_drugs_and_dates_of_administration', 
                     'f23_other_relevant_history', 'f24a_name_and_address_of_manufacturer']:
            if field in cioms_data and cioms_data[field] and len(cioms_data[field]) > 500:
                cioms_data[f'{field}_continued'] = True
        
        if 'f14_21_suspect_drugs' in cioms_data and cioms_data['f14_21_suspect_drugs']:
            cioms_data['reactions_data'] = []
            for i, reaction in enumerate(cioms_data.get('e_i_reaction_event', [])):
                reaction_data = {
                    'seq_no': i + 1,
                    'description': reaction.get('e_i_1_2_reaction_primary_source_translation', ''),
                    'start_date': reaction.get('e_i_4_date_start_reaction', ''),
                    'meddra_code': reaction.get('e_i_2_1b_reaction_meddra_code', ''),
                    'meddra_version': reaction.get('e_i_2_1a_meddra_version_reaction', '')
                }
                cioms_data['reactions_data'].append(reaction_data)
        
        return render(request, 'cioms.html', cioms_data)


class MedDRAReleaseView(View):
    meddra_service: MedDRAServiceProtocol = ...

    def get(self, request: http.HttpRequest) -> http.HttpResponse:
        objects = self.meddra_service.list()
        response = meddra.ReleaseResponse(
            root=[meddra.Release(id=obj.id, version=obj.version, language=obj.language) for obj in objects]
        )
        return http.HttpResponse(response.model_dump_json(), status=HTTPStatus.OK, content_type='application/json')


class MedDRASearchView(View):
    meddra_service: MedDRAServiceProtocol = ...

    def post(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        search_request = meddra.SearchRequest.parse_raw(request.body)
        objects = self.meddra_service.search(search_request.search.level,
                                             search_request.state,
                                             search_request.search.input,
                                             pk)
        response = meddra.SearchResponse(terms=[meddra.Term(code=obj.code, name=obj.name) for obj in objects],
                                         level=search_request.search.level)
        return http.HttpResponse(response.model_dump_json(), status=HTTPStatus.OK, content_type='application/json')


class CodeSetView(View):
    code_set_service: CodeSetServiceProtocol = ...

    def get(self, request: http.HttpRequest, codeset: str) -> http.HttpResponse:
        objects = self.code_set_service.search(codeset,
                                               request.GET.get('q', ''),
                                               request.GET.get('lang', 'ENG'),
                                               request.GET.get('property', None))
        response = code_set.SearchResponse([code_set.Term(code=obj.code, name=obj.name) for obj in objects])
        return http.HttpResponse(response.model_dump_json(), status=HTTPStatus.OK, content_type='application/json')

    def post(self, request: http.HttpRequest, codeset: str) -> http.HttpResponse:
        file = request.FILES.get('file')
        if not file:
            return http.HttpResponse(status=HTTPStatus.BAD_REQUEST, content="File not uploaded")

        self.code_set_service.create(codeset, file, request.POST.get('lang', 'ENG'))
        return http.HttpResponse(status=HTTPStatus.CREATED)


class ExportMultipleXmlView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        try:
            data = json.loads(request.body)
            icsr_ids = data.get('ids', [])
            
            if not icsr_ids:
                return http.HttpResponse('No ICSR IDs provided', status=HTTPStatus.BAD_REQUEST)
            
            icsr_list = []
            for icsr_id in icsr_ids:
                try:
                    icsr = self.domain_service.read(self.model_class, icsr_id)
                    icsr_list.append(icsr)
                except Exception as e:
                    print(f"Error retrieving ICSR {icsr_id}: {str(e)}")
                    continue
            
            if not icsr_list:
                return http.HttpResponse('None of the requested ICSRs could be retrieved', 
                                        status=HTTPStatus.NOT_FOUND)
            
            combined_xml = self.convert_multiple_to_xml(icsr_list)
            
            filename = f"e2b_export_{icsr_list[0].id}_{len(icsr_list)}_records_{djtz.now().strftime('%Y%m%d%H%M%S')}.xml"
            
            response = http.HttpResponse(combined_xml, content_type='application/xml')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            
            return response
            
        except Exception as e:
            traceback.print_exc()
            print(f"Error in ExportMultipleXmlView: {str(e)}")
            return http.HttpResponse(f'Error processing request: {str(e)}', 
                                    status=HTTPStatus.INTERNAL_SERVER_ERROR)
    
    @staticmethod
    def find(root, field):
        ns = {
            'hl7': "urn:hl7-org:v3",
        }
        res = root.findall(f'hl7:{field}', ns)
        if len(res) == 1:
            return res[0]
        elif len(res) > 1:
            return res 

    @staticmethod
    def set_id_sender_receiver_creation_time(root):
        ExportMultipleXmlView.find(root, 'id').set('extension', str(uuid.uuid4()))

        for agent in ['sender', 'receiver']:
            agent_xml = ExportMultipleXmlView.find(root, agent)
            device = ExportMultipleXmlView.find(agent_xml, 'device')
            id_element = ExportMultipleXmlView.find(device, 'id')
            id_element.set('extension', str(uuid.uuid4()))

        ExportMultipleXmlView.find(root, 'creationTime').set("value", datetime.now().strftime('%Y%m%d%H%M%S'))

    
    def convert_multiple_to_xml(self, icsr_list: list) -> str: 
        parser = etree.XMLParser(remove_blank_text=False)
        module_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir, _ = module_dir.split('/', 1)
        E2B_tamplate = os.path.join(*[
            root_dir,
            'app',
            'templates',
            'E2B.xml'
        ])
        tree = etree.parse(E2B_tamplate, parser)
        root = tree.getroot()

        # N.1.(2,3,4,5)
        self.set_id_sender_receiver_creation_time(root)

        template_message_part = self.find(root, 'PORR_IN049016UV')
        root.remove(template_message_part)

        for icsr in icsr_list:
            template_copy = copy.deepcopy(template_message_part)
            new_element = self.convert_single_to_xml(icsr, template_copy)
            root.append(new_element)

        return etree.tostring(
            tree,
            xml_declaration=True,
            encoding='utf-8',
            pretty_print=True
        )

    def convert_single_to_xml(self, icsr, root):
        def set_icsr_field(root, key, obj, field, get_value=lambda x: str(x.value)):
            if obj is not None and hasattr(obj, field) and getattr(obj, field) is not None:
                if key is not None:
                    root.set(key, get_value(getattr(obj, field)))
                else:
                    root.text = get_value(getattr(obj, field))

        def set_text_icsr_field_with_null(root, key, obj, field, get_value=lambda x: str(x.value)):
            params = {"key": key, "field": field, "get_value": get_value}
            if (
                obj is not None and 
                hasattr(obj, field) and
                getattr(obj, field) is not None and
                hasattr(getattr(obj, field), "null_flavor") and 
                getattr(obj, field).null_flavor is not None
            ):
                params['key'] = "nullFlavor"
                params["get_value"] = lambda x: str(x.null_flavor.value)

            return set_icsr_field(root, obj=obj, **params)
        
        # N.2.r.(1,2,3,4)
        self.set_id_sender_receiver_creation_time(root)

        c1 = icsr.c_1_identification_case_safety_report
        c2 = icsr.c_2_r_primary_source_information
        c3 = icsr.c_3_information_sender_case_safety_report
        c4 = icsr.c_4_r_literature_reference
        c5 = icsr.c_5_study_identification
        d = icsr.d_patient_characteristics
        ei = icsr.e_i_reaction_event
        fr = icsr.f_r_results_tests_procedures_investigation_patient
        gk = icsr.g_k_drug_information
        h = icsr.h_narrative_case_summary

        # N.2.r.1
        set_icsr_field(self.find(root, 'id'), "extension", c1, "c_1_1_sender_safety_report_unique_id")

        control_act_process = self.find(root, "controlActProcess")
        # C.1.2
        set_icsr_field(self.find(control_act_process, "effectiveTime"), 'value', c1, "c_1_2_date_creation")

        investigationEvent = self.find(self.find(control_act_process, "subject"), "investigationEvent")
        # C.1.1
        set_icsr_field(self.find(investigationEvent, 'id[@root="2.16.840.1.113883.3.989.2.1.3.1"]'), "extension", c1, "c_1_1_sender_safety_report_unique_id")

        # C.1.8.1
        set_icsr_field(self.find(investigationEvent, 'id[@root="2.16.840.1.113883.3.989.2.1.3.2"]'), "extension", c1, "c_1_8_1_worldwide_unique_case_identification_number")

        # H.1
        set_icsr_field(self.find(investigationEvent, 'text'), None, h, "h_1_case_narrative")

        # C.1.4
        set_icsr_field(
            self.find(self.find(investigationEvent, "effectiveTime"), "low"),
            "value",
            c1,
            "c_1_4_date_report_first_received_source"
        )

        # C.1.5 
        set_icsr_field(
            self.find(investigationEvent, "availabilityTime"),
            "value",
            c1,
            "c_1_5_date_most_recent_information"
        )

        references = self.find(investigationEvent, "reference")
        for reference in references:
            document = self.find(reference, "document")
            codeDocument = self.find(document, "code").get("code")
            if codeDocument == 1:
                investigationEvent.remove(reference)
                if c1 is not None:
                    for documents_held_sender in c1.c_1_6_1_r_documents_held_sender:
                        reference_copy = copy.deepcopy(reference)
                        title = self.find(self.find(reference_copy, "document"), "title")
                        # C.1.6.1.r.1
                        set_icsr_field(title, None, documents_held_sender, "c_1_6_1_r_1_documents_held_sender")
                        investigationEvent.append(reference_copy)
            elif codeDocument == 2:
                investigationEvent.remove(reference)
                for literature_reference in c4:
                    reference_copy = copy.deepcopy(reference)
                    bibliographicDesignationText = self.find(self.find(reference_copy, "document"), "bibliographicDesignationText")
                    # C.4.r.1
                    set_text_icsr_field_with_null(bibliographicDesignationText, None, c4, "c_4_r_1_literature_reference")
                    investigationEvent.append(reference_copy) 
            
        components_big = self.find(investigationEvent, 'component')
        for component_big in components_big:
            adverseEventAssessment = self.find(component_big, "adverseEventAssessment")
            observationEvent = self.find(component_big, "observationEvent")
            if adverseEventAssessment is not None:
                primaryRole = self.find(self.find(adverseEventAssessment, "subject1"), "primaryRole")
                player1 = self.find(primaryRole, "player1")
                
                # D.1
                set_text_icsr_field_with_null(self.find(player1, "name"), None, d, "d_1_patient")
                
                # D.5 
                set_text_icsr_field_with_null(self.find(player1, "administrativeGenderCode"), "code", d, "d_5_sex")

                # D.2.1
                set_text_icsr_field_with_null(self.find(player1, "birthTime"), "value", d, "d_2_1_date_birth")

                # D.9.1
                set_text_icsr_field_with_null(self.find(player1, "deceasedTime"), "value", d, "d_9_1_date_death")

                asIdentifiedEntitys = self.find(player1, "asIdentifiedEntity")
                for asIdentifiedEntity in asIdentifiedEntitys:
                    codeTmp = self.find(asIdentifiedEntity, "code").get("code")
                    if codeTmp == "1":
                        # D.1.1.1
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_1_medical_record_number_source_gp"
                        )
                    elif codeTmp == "2":
                        # D.1.1.2
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_2_medical_record_number_source_specialist"
                        )
                    elif codeTmp == "3":
                        # D.1.1.3
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_3_medical_record_number_source_hospital"
                        )
                    elif codeTmp == "4":
                        # D.1.1.4
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_4_medical_record_number_source_investigation"
                        )

                role = self.find(player1, "role")
                associatedPersonRole = self.find(role, "associatedPerson")
                # D.10.1
                set_text_icsr_field_with_null(self.find(associatedPersonRole, "name"), None, d, "d_10_1_parent_identification")
                # D.10.6
                set_text_icsr_field_with_null(self.find(associatedPersonRole, "administrativeGenderCode"), "code", d, "d_10_6_sex_parent")
                # D.10.2.1
                set_text_icsr_field_with_null(self.find(associatedPersonRole, "birthTime"), "value", d, "d_10_2_1_date_birth_parent")
            
                subjectOf2Roles = self.find(role, "subjectOf2")
                for subjectOf2Role in subjectOf2Roles:
                    observationSubjectOf2 = self.find(subjectOf2Role, "observation")
                    organizerSubjectOf2 = self.find(subjectOf2Role, "organizer")
                    if observationSubjectOf2 is not None:
                        codeObservationSubjectOf2 = self.find(observationSubjectOf2, "code").get("code")
                        if codeObservationSubjectOf2 == "3":
                            # D.10.2.2a
                            set_icsr_field(self.find(observationSubjectOf2, "value"), "value", d, "d_10_2_2a_age_parent_num")
                            # D.10.2.2b
                            set_icsr_field(self.find(observationSubjectOf2, "value"), "unit", d, "d_10_2_2b_age_parent_unit")
                        elif codeObservationSubjectOf2 == "22":
                            # D.10.3
                            set_text_icsr_field_with_null(self.find(observationSubjectOf2, "value"), "value", d, "d_10_3_last_menstrual_period_date_parent")
                        elif codeObservationSubjectOf2 == "7":
                            # D.10.4
                            set_text_icsr_field_with_null(self.find(observationSubjectOf2, "value"), "value", d, "d_10_4_body_weight_parent")
                        elif codeObservationSubjectOf2 == "17":
                            # D.10.5
                            set_text_icsr_field_with_null(self.find(observationSubjectOf2, "value"), "value", d, "d_10_5_height_parent")
                    elif organizerSubjectOf2 is not None:
                        codeOrganizerSubjectOf2 = self.find(organizerSubjectOf2, "code").get("code")
                        if codeOrganizerSubjectOf2 == "1":
                            componentsSmall = self.find(organizerSubjectOf2, "component")
                            for componentSmall in componentsSmall:
                                observationLocal = self.find(componentSmall, "observation")
                                codeSystemObservationLocal = self.find(observationLocal, "code").get("codeSystem")
                                if codeSystemObservationLocal == "2.16.840.1.113883.6.163":
                                    organizerSubjectOf2.remove(componentSmall)
                                    if d is not None:
                                        for parent_info in d.d_10_7_1_r_structured_information_parent_meddra_code:
                                            componentSmallCopy = copy.deepcopy(componentSmall)
                                            observationLocalSmaller = self.find(componentSmallCopy, "observation")
                                            # D.10.7.1.r.1a
                                            set_icsr_field(self.find(observationLocalSmaller, "code"), "codeSystemVersion", parent_info, "d_10_7_1_r_1a_meddra_version_medical_history")
                                            # D.10.7.1.r.1b
                                            set_icsr_field(self.find(observationLocalSmaller, "code"), "code", parent_info, "d_10_7_1_r_1b_medical_history_meddra_code")
                                            effectiveTimeLocalSmaller = self.find(observationLocalSmaller, "effectiveTime")
                                            # D.10.7.1.r.2
                                            set_text_icsr_field_with_null(self.find(effectiveTimeLocalSmaller, "low"), "value", parent_info, "d_10_7_1_r_2_start_date")
                                            # D.10.7.1.r.4
                                            set_text_icsr_field_with_null(self.find(effectiveTimeLocalSmaller, "high"), "value", parent_info, "d_10_7_1_r_4_end_date")
                                            outboundRelationship2LocalSmaller = self.find(observationLocalSmaller, "outboundRelationship2")
                                            observationOutboundRelationship2LocalSmaller = self.find(outboundRelationship2LocalSmaller, "observation")
                                            # D.10.7.1.r.5
                                            set_icsr_field(self.find(observationOutboundRelationship2LocalSmaller, "value"), None, parent_info, "d_10_7_1_r_5_comments")
                                            inboundRelationship2LocalSmaller = self.find(observationLocalSmaller, "inboundRelationship")
                                            observationOutboundRelationship2LocalSmaller = self.find(inboundRelationship2LocalSmaller, "observation")
                                            # D.10.7.1.r.3
                                            set_text_icsr_field_with_null(self.find(observationOutboundRelationship2LocalSmaller, "value"), "value", parent_info, "d_10_7_1_r_3_continuing")
                                            organizerSubjectOf2.append(componentSmallCopy)
                                elif codeSystemObservationLocal == "2.16.840.1.113883.3.989.2.1.1.19":
                                    # D.10.7.2
                                    set_icsr_field(self.find(observationLocal, "value"), None, d, "d_10_7_2_text_medical_history_parent")
                        elif codeOrganizerSubjectOf2 == "2":
                            componentSmall = self.find(organizerSubjectOf2, "component")
                            organizerSubjectOf2.remove(componentSmall)
                            if d is not None:
                                for drug_history_parent in d.d_10_8_r_past_drug_history_parent:
                                    componentSmallCopy = copy.deepcopy(componentSmall)
                                    substanceAdministrationSmall = self.find(componentSmallCopy, "substanceAdministration")
                                    effectiveTimeSmall = self.find(substanceAdministrationSmall, "effectiveTime")
                                    # D.10.8.r.4
                                    set_text_icsr_field_with_null(self.find(effectiveTimeSmall, "low"), "value", drug_history_parent, "d_10_8_r_4_start_date")
                                    # D.10.8.r.5
                                    set_text_icsr_field_with_null(self.find(effectiveTimeSmall, "high"), "value", drug_history_parent, "d_10_8_r_5_end_date")
                                    kindOfProductSmall = self.find(self.find(self.find(substanceAdministrationSmall, "consumable"), "instanceOfKind"), "kindOfProduct")
                                    if drug_history_parent is not None and drug_history_parent.d_10_8_r_2a_mpid_version is not None:
                                        # D.10.8.r.2a
                                        set_icsr_field(self.find(kindOfProductSmall, "code"), "code", drug_history_parent, "d_10_8_r_2a_mpid_version")
                                        # D.10.8.r.2b
                                        set_icsr_field(self.find(kindOfProductSmall, "code"), "codeSystemVersion", drug_history_parent, "d_10_8_r_2b_mpid")
                                    else:
                                        # D.10.8.r.3a
                                        set_icsr_field(self.find(kindOfProductSmall, "code"), "code", drug_history_parent, "d_10_8_r_3a_phpid_version")
                                        # D.10.8.r.3b
                                        set_icsr_field(self.find(kindOfProductSmall, "code"), "codeSystemVersion", drug_history_parent, "d_10_8_r_3b_phpid")
                                    # D.10.8.r.1
                                    set_icsr_field(self.find(kindOfProductSmall, "name"), None, drug_history_parent, "d_10_8_r_1_name_drug")
                                    outboundRelationshipsSmall2 = self.find(substanceAdministrationSmall, "outboundRelationship2")
                                    for outboundRelationshipSmall2 in outboundRelationshipsSmall2:
                                        observationOutboundRelationshipsSmall2 = self.find(outboundRelationshipSmall2, "observation")
                                        codeTmp = self.find(observationOutboundRelationshipsSmall2, "code").get("code")
                                        if codeTmp == "19":
                                            # D.10.8.r.6a
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", componentSmallCopy, "d_10_8_r_6a_meddra_version_indication")
                                            # D.10.8.r.6b
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", componentSmallCopy, "d_10_8_r_6b_indication_meddra_code")
                                        elif codeTmp == "29":
                                            # D.10.8.r.7a
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", componentSmallCopy, "d_10_8_r_7a_meddra_version_reaction")
                                            # D.10.8.r.7b
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", componentSmallCopy, "d_10_8_r_7b_reactions_meddra_code")
                                    organizerSubjectOf2.append(componentSmallCopy)                                           

                subjectOf1 = self.find(primaryRole, "subjectOf1")
                researchStudy = self.find(subjectOf1, "researchStudy")
                # C.5.3
                set_text_icsr_field_with_null(
                    self.find(researchStudy, "id"), 
                    "extension",
                    c5, 
                    "c_5_3_sponsor_study_number"
                )

                # C.5.4
                set_icsr_field(self.find(researchStudy, "code"), "code", c5, "c_5_4_study_type_reaction")

                # C.5.2
                set_text_icsr_field_with_null(
                    self.find(researchStudy, "title"),
                    None,
                    c5,
                    "c_5_2_study_name"
                )
                authorization = self.find(researchStudy, "authorization")
                researchStudy.remove(authorization)
                if c5 is not None:
                    for study_registration in c5.c_5_1_r_study_registration:
                        authorizationCopy = copy.deepcopy(authorization)
                        studyRegistration = self.find(authorizationCopy, "studyRegistration")
                        # C.5.1.r.1
                        set_text_icsr_field_with_null(self.find(studyRegistration, "id"), "extension", study_registration, "c_5_1_r_1_study_registration_number")
                        # C.5.1.r.2
                        set_text_icsr_field_with_null(
                            self.find(
                                self.find(
                                    self.find(
                                        self.find(studyRegistration, "author"),
                                        "territorialAuthority"
                                    ),
                                    "governingPlace"
                                ),
                                "code"
                            ),
                            "code",
                            study_registration,
                            "c_5_1_r_2_study_registration_country"
                        )
                        researchStudy.append(authorizationCopy)
                
                subjectOf2s = self.find(primaryRole, "subjectOf2")
                for subjectOf2 in subjectOf2s:
                    observation = self.find(subjectOf2, "observation")
                    organizer = self.find(subjectOf2, "organizer")
                    if observation is not None:
                        code_subjectOf2 = self.find(observation, "code")
                        code_subjectOf2_code_value = code_subjectOf2.get("code")
                        val = self.find(observation, "value")
                        if code_subjectOf2_code_value == "3":
                            # D.2.2a
                            set_icsr_field(val, "value", d, "d_2_2a_age_onset_reaction_num")
                            # D.2.2b
                            set_icsr_field(val, "unit", d, "d_2_2b_age_onset_reaction_unit")
                        elif code_subjectOf2_code_value == "16":
                            # D.2.2.1a
                            set_icsr_field(val, "value", d, "d_2_2_1a_gestation_period_reaction_foetus_num")
                            # D.2.2.1b
                            set_icsr_field(val, "unit", d, "d_2_2_1b_gestation_period_reaction_foetus_unit")
                        elif code_subjectOf2_code_value == "4":
                            # D.2.3 
                            set_icsr_field(val, "code", d, "d_2_3_patient_age_group")
                        elif code_subjectOf2_code_value == "7":
                            # D.3
                            set_icsr_field(val, "value", d, "d_3_body_weight")
                        elif code_subjectOf2_code_value == "17":
                            # D.4
                            set_icsr_field(val, "value", d, "d_4_height")
                        elif code_subjectOf2_code_value == "22":
                            # D.6
                            set_icsr_field(val, "value", d, "d_6_last_menstrual_period_date")
                        elif code_subjectOf2_code_value == "32":
                            primaryRole.remove(subjectOf2)
                            if d is not None:
                                for cause_death in d.d_9_2_r_cause_death:
                                    subjectOf2Copy = copy.deepcopy(subjectOf2)
                                    valueLocal = self.find(self.find(subjectOf2Copy, "observation"), "value")
                                    # D.9.2.r.1a
                                    set_icsr_field(valueLocal, "codeSystemVersion", cause_death, "d_9_2_r_1a_meddra_version_cause_death")
                                    # D.9.2.r.1b
                                    set_icsr_field(valueLocal, "code", cause_death, "d_9_2_r_1b_cause_death_meddra_code")
                                    # D.9.2.r.2
                                    set_icsr_field(self.find(valueLocal, "originalText"), None, cause_death, "d_9_2_r_2_cause_death")
                                    primaryRole.append(subjectOf2Copy)
                        elif code_subjectOf2_code_value == "5":
                            # D.9.3
                            set_text_icsr_field_with_null(val, "value", d, "d_9_3_autopsy")
                            outboundRelationship2Local = self.find(observation, "outboundRelationship2")
                            observation.remove(outboundRelationship2Local)
                            if d is not None:
                                for determined_cause_death in d.d_9_4_r_autopsy_determined_cause_death:
                                    outboundRelationship2LocalCopy = copy.deepcopy(outboundRelationship2Local)
                                    valueLocal = self.find(self.find(outboundRelationship2LocalCopy, "observation"), "value")
                                    # D.9.4.r.1a
                                    set_icsr_field(valueLocal, "codeSystemVersion", determined_cause_death, "d_9_4_r_1a_meddra_version_autopsy_determined_cause_death")
                                    # D.9.4.r.1b
                                    set_icsr_field(valueLocal, "code", determined_cause_death, "d_9_4_r_1b_autopsy_determined_cause_death_meddra_code")
                                    # D.9.4.r.2
                                    set_icsr_field(self.find(valueLocal, "originalText"), None, determined_cause_death, "d_9_4_r_2_autopsy_determined_cause_death")
                                    observation.append(outboundRelationship2LocalCopy)
                        elif code_subjectOf2_code_value == "29":
                            # reaction section
                            primaryRole.remove(subjectOf2)
                            for i, reaction in enumerate(ei, start=1):
                                subjectOf2Copy = copy.deepcopy(subjectOf2)
                                observation_new = self.find(subjectOf2Copy, "observation")
                                self.find(observation_new, "id").set("root", f'r-id{i}')

                                effectiveTime = self.find(observation_new, "effectiveTime")
                                low = self.find(effectiveTime, "low")
                                high = self.find(effectiveTime, "high")
                                width = self.find(effectiveTime, "width")
                                
                                # E.i.4
                                set_icsr_field(low, "value", reaction, "e_i_4_date_start_reaction")
                                # E.i.5
                                set_icsr_field(high, "value", reaction, "e_i_5_date_end_reaction")
                                # E.i.6a
                                set_icsr_field(width, "value", reaction, "e_i_6a_duration_reaction_num")
                                # E.i.6b
                                set_icsr_field(width, "unit", reaction, "e_i_6b_duration_reaction_unit")
                                value = self.find(observation_new, "value")
                                # E.i.2.1a
                                set_icsr_field(value, "codeSystemVersion", reaction, "e_i_2_1a_meddra_version_reaction")
                                # E.i.2.1b
                                set_icsr_field(value, "code", reaction, "e_i_2_1b_reaction_meddra_code")
                                originalText = self.find(value, "originalText")
                                # E.i.1.1a
                                set_icsr_field(originalText, None, reaction, "e_i_1_1a_reaction_primary_source_native_language")
                                # E.i.1.1b
                                set_icsr_field(originalText, "language", reaction, "e_i_1_1b_reaction_primary_source_language")

                                # E.i.9 Identification of the Country Where the Reaction Occurred
                                location = self.find(observation_new, "location")
                                locationCode = self.find(self.find(self.find(location, "locatedEntity"), "locatedPlace"), "code")
                                set_icsr_field(locationCode, "code", reaction, "e_i_9_identification_country_reaction")
                            
                                outboundRelationships2 = self.find(observation_new, "outboundRelationship2")
                                for outboundRelationship2 in outboundRelationships2:
                                    observationOutboundRelationship = self.find(outboundRelationship2, "observation")
                                    codeOutboundRelationship = self.find(observationOutboundRelationship, "code")
                                    valueOutboundRelationship = self.find(observationOutboundRelationship, "value")
                                    if codeOutboundRelationship == "30":
                                        # E.i.1.2
                                        set_icsr_field(valueOutboundRelationship, None, reaction, "e_i_1_2_reaction_primary_source_translation")
                                    elif codeOutboundRelationship == "37":
                                        # E.i.3.1
                                        set_icsr_field(valueOutboundRelationship, "code", reaction, "e_i_3_1_term_highlighted_reporter") 
                                    elif codeOutboundRelationship == "34":
                                        # E.i.3.2a
                                        set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2a_results_death") 
                                    elif codeOutboundRelationship == "21":
                                        # E.i.3.2b
                                        set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2b_life_threatening") 
                                    elif codeOutboundRelationship == "33":
                                        # E.i.3.2c
                                        set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2c_caused_prolonged_hospitalisation") 
                                    elif codeOutboundRelationship == "35":
                                        # E.i.3.2d
                                        set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2d_disabling_incapacitating") 
                                    elif codeOutboundRelationship == "12":
                                        # E.i.3.2e
                                        set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2e_congenital_anomaly_birth_defect")                    
                                    elif codeOutboundRelationship == "26":
                                        # E.i.3.2f
                                        set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2f_other_medically_important_condition")
                                    elif codeOutboundRelationship == "27":
                                        # E.i.7
                                        set_icsr_field(valueOutboundRelationship, "code", reaction, "e_i_7_outcome_reaction_last_observation")
                                    elif codeOutboundRelationship == "24":
                                        # E.i.8
                                        set_icsr_field(valueOutboundRelationship, "value", reaction, "e_i_8_medical_confirmation_healthcare_professional")  
                                primaryRole.append(subjectOf2Copy)
                    if organizer is not None:
                        code_subjectOf2 = self.find(organizer, "code").get("code")
                        if code_subjectOf2 == "1":
                            components = self.find(organizer, "component")
                            for component in components:
                                codeTmp = self.find(self.find(component, "observation"), "code")
                                if codeTmp.get("codeSystem") == "2.16.840.1.113883.6.163":
                                    organizer.remove(component)
                                    if d is not None:                                            
                                        for information_medical_history in d.d_7_1_r_structured_information_medical_history:
                                            component_new = copy.deepcopy(component)
                                            observationOrganizer = self.find(component_new, "observation")
                                            codeOservationOrganizer = self.find(observationOrganizer, "code")
                                            # D.7.1.r.1a:
                                            set_icsr_field(codeOservationOrganizer, "codeSystemVersion", information_medical_history, "d_7_1_r_1a_meddra_version_medical_history")
                                            # D.7.1.r.1b
                                            set_icsr_field(codeOservationOrganizer, "code", information_medical_history, "d_7_1_r_1b_medical_history_meddra_code")
                                            # D.7.1.r.2
                                            set_text_icsr_field_with_null(self.find(self.find(observationOrganizer, "effectiveTime"), "low"), "value", information_medical_history, "d_7_1_r_2_start_date")
                                            # D.7.1.r.4
                                            set_text_icsr_field_with_null(self.find(self.find(observationOrganizer, "effectiveTime"), "high"), "value", information_medical_history, "d_7_1_r_4_end_date")
                                            for tmpOutboundRelationship2 in self.find(observationOrganizer, "outboundRelationship2"):
                                                codeTmpLocal = self.find(self.find(tmpOutboundRelationship2, "observation"), "code").get("code")
                                                valTmp = self.find(self.find(tmpOutboundRelationship2, "observation"), "value")
                                                if codeTmpLocal == "10":
                                                    set_icsr_field(valTmp, None, information_medical_history, "d_7_1_r_5_comments")                                                    
                                                elif codeTmpLocal == "38":
                                                    set_icsr_field(valTmp, "value", information_medical_history, "d_7_1_r_6_family_history")
                                            # D.7.1.r.3
                                            set_text_icsr_field_with_null(
                                                self.find(self.find(self.find(observationOrganizer, "inboundRelationship"), "observation"), "value"),
                                                "value",
                                                information_medical_history,
                                                "d_7_1_r_3_continuing"
                                            )
                                            organizer.append(component_new)
                                elif codeTmp.get("code") == "18":
                                    # D.7.2
                                    val = self.find(self.find(component, "observation"), "value")
                                    set_text_icsr_field_with_null(val, None, d, "d_7_2_text_medical_history")
                                elif codeTmp.get("code") == "11":
                                    # D.7.3
                                    val = self.find(self.find(component, "observation"), "value")
                                    set_icsr_field(val, "value", d, "d_7_3_concomitant_therapies")
                        if code_subjectOf2 == "2":
                            component = self.find(organizer, "component")
                            organizer.remove(component)
                            if d is not None:
                                for drug_history in d.d_8_r_past_drug_history:
                                    component_new = copy.deepcopy(component)
                                    substanceAdministration = self.find(component_new, "substanceAdministration")
                                    effectiveTime = self.find(substanceAdministration, "effectiveTime")
                                    lowLocal = self.find(effectiveTime, "low")
                                    highLocal = self.find(effectiveTime, "high")
                                    # D.8.r.4
                                    set_text_icsr_field_with_null(lowLocal, "value", drug_history, "d_8_r_4_start_date")
                                    # D.8.r.5
                                    set_text_icsr_field_with_null(highLocal, "value", drug_history, "d_8_r_5_end_date")
                                    kindOfProduct = self.find(self.find(self.find(substanceAdministration, "consumable"), "instanceOfKind"), "kindOfProduct")
                                    if drug_history is not None and drug_history.d_8_r_2a_mpid_version is not None:
                                        # D.8.r.2a
                                        set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_2a_mpid_version")
                                        # D.8.r.2b
                                        set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_2b_mpid")
                                    else:
                                        # D.8.r.3a
                                        set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_3a_phpid_version")
                                        # D.8.r.3b
                                        set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_3b_phpid")
                                    # D.8.r.1
                                    set_icsr_field(self.find(kindOfProduct, "name"), None, drug_history, "d_8_r_1_name_drug")
                                    outboundRelationshipsSmall2 = self.find(substanceAdministration, "outboundRelationship2")
                                    for outboundRelationshipSmall2 in outboundRelationshipsSmall2:
                                        observationOutboundRelationshipsSmall2 = self.find(outboundRelationshipSmall2, "observation")
                                        codeTmp = self.find(observationOutboundRelationshipsSmall2, "code").get("code")
                                        if codeTmp == "19":
                                            # D.8.r.6a
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", drug_history, "d_8_r_6a_meddra_version_indication")
                                            # D.8.r.6b
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", drug_history, "d_8_r_6b_indication_meddra_code")
                                        elif codeTmp == "29":
                                            # D.8.r.7a
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", drug_history, "d_8_r_7a_meddra_version_reaction")
                                            # D.8.r.7b
                                            set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", drug_history, "d_8_r_7b_reaction_meddra_code")
                                    organizer.append(component_new)
                        elif code_subjectOf2 == "3":
                            component = self.find(organizer, "component")
                            organizer.remove(component)
                            for tests_procedures in fr:
                                componentTmp = copy.deepcopy(component)
                                observationTmp = self.find(componentTmp, "observation")
                                codeTmp = self.find(observationTmp, "code")
                                # F.r.2.2a
                                set_icsr_field(codeTmp, "codeSystemVersion", tests_procedures, "f_r_2_2a_meddra_version_test_name")
                                # F.r.2.2b
                                set_icsr_field(codeTmp, "code", tests_procedures, "f_r_2_2b_test_name_meddra_code")
                                # F.r.2.1
                                set_icsr_field(self.find(codeTmp, "originalText"), None, tests_procedures, "f_r_2_1_test_name")
                                # F.r.1
                                set_icsr_field(self.find(observationTmp, "effectiveTime"), "value", tests_procedures, "f_r_1_test_date")
                                valTmp = self.find(observationTmp, "value")
                                willfill = self.find(valTmp, "center")
                                if tests_procedures.f_r_3_2_test_result_val_qual is not None:
                                    # F.r.3.2
                                    set_text_icsr_field_with_null(willfill, "value", tests_procedures, "f_r_3_2_test_result_val_qual")
                                    # F.r.3.3
                                    set_icsr_field(willfill, "unit", tests_procedures, "f_r_3_3_test_result_unit") 
                                else:
                                    # F.r.3.4
                                    valTmp.remove(willfill)
                                    set_icsr_field(valTmp, None, tests_procedures, "f_r_3_4_result_unstructured_data")                               
                                # F.r.3.1
                                set_icsr_field(self.find(observationTmp, "interpretationCode"), "code", tests_procedures, "f_r_3_1_test_result_code")                                                          
                                for referenceRange in self.find(observationTmp, "referenceRange"):
                                    interpretationCodeTmp = self.find(self.find(referenceRange, "observationRange"), "interpretationCode").get("code")
                                    valReferenceRange = self.find(self.find(referenceRange, "observationRange"), "value")
                                    if interpretationCodeTmp == "L":
                                        # F.r.4
                                        set_icsr_field(valReferenceRange, "value", tests_procedures, "f_r_4_normal_low_value")
                                    elif interpretationCodeTmp == "H":
                                        # F.r.5
                                        set_icsr_field(valReferenceRange, "value", tests_procedures, "f_r_5_normal_high_value")
                                for iter in self.find(observationTmp, "outboundRelationship2"):
                                    interpretationCodeTmp = self.find(self.find(iter, "observation"), "code").get("code")
                                    valReferenceRange = self.find(self.find(iter, "observation"), "value")
                                    if interpretationCodeTmp == "10":
                                        # F.r.6
                                        set_icsr_field(valReferenceRange, None, tests_procedures, "f_r_6_comments")
                                    elif interpretationCodeTmp == "25":
                                        # F.r.7
                                        set_icsr_field(valReferenceRange, "value", tests_procedures, "f_r_7_more_information_available")
                                organizer.append(componentTmp)
                        elif code_subjectOf2 == "4":
                            # drugInformation
                            component = self.find(organizer, "component")
                            organizer.remove(component)
                            for i, drug_info in enumerate(gk, start=1):
                                component_new = copy.deepcopy(component)
                                substanceAdministration = self.find(component_new, "substanceAdministration")                                
                                instanceOfKind = self.find(self.find(substanceAdministration, "consumable"), "instanceOfKind")
                                kindOfProduct = self.find(instanceOfKind, "kindOfProduct")
                                if drug_info is not None and drug_info.g_k_2_1_1a_mpid_version is not None:
                                    # G.k.2.1.1a
                                    set_icsr_field(self.find(kindOfProduct, "code"), "codeSystemVersion", drug_info, "g_k_2_1_1a_mpid_version")
                                    # G.k.2.1.1b
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_info, "g_k_2_1_1b_mpid")
                                else:
                                    # G.k.2.1.2a
                                    set_icsr_field(self.find(kindOfProduct, "code"), "codeSystemVersion", drug_info, "g_k_2_1_2a_phpid_version")
                                    # G.k.2.1.2b
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_info, "g_k_2_1_2b_phpid")
                                # G.k.2.2
                                set_icsr_field(self.find(kindOfProduct, "name"), None, drug_info, "g_k_2_2_medicinal_product_name_primary_source")
                                approval = self.find(self.find(self.find(kindOfProduct, "asManufacturedProduct"), "subjectOf"), "approval")
                                # G.k.3.1
                                set_icsr_field(self.find(approval, "id"), "extension", drug_info, "g_k_3_1_authorisation_application_number")
                                # G.k.3.3
                                set_icsr_field(
                                    self.find(self.find(self.find(self.find(approval, "holder"), "role"), "playingOrganization"), "name"), 
                                    None, 
                                    drug_info, 
                                    "g_k_3_3_name_holder_applicant"
                                )
                                # G.k.3.2
                                set_icsr_field(
                                    self.find(self.find(self.find(self.find(approval, "author"), "territorialAuthority"), "territory"), "code"), 
                                    "code", 
                                    drug_info, 
                                    "g_k_3_2_country_authorisation_application"
                                )

                                ingredient = self.find(kindOfProduct, "ingredient")
                                kindOfProduct.remove(ingredient)
                                for substance in drug_info.g_k_2_3_r_substance_id_strength:
                                    ingredient_new = copy.deepcopy(ingredient)
                                    numerator = self.find(self.find(ingredient_new, "quantity"), "numerator")
                                    # G.k.2.3.r.3a
                                    set_icsr_field(numerator, "value", substance, "g_k_2_3_r_3a_strength_num")
                                    # G.k.2.3.r.3b
                                    set_icsr_field(numerator, "unit", substance, "g_k_2_3_r_3b_strength_unit")
                                    # G.k.2.3.r.2a
                                    set_icsr_field(self.find(self.find(ingredient_new, "ingredientSubstance"), "code"), "codeSystemVersion", substance, "g_k_2_3_r_2a_substance_termid_version")
                                    # G.k.2.3.r.2b
                                    set_icsr_field(self.find(self.find(ingredient_new, "ingredientSubstance"), "code"), "code", substance, "g_k_2_3_r_2b_substance_termid")
                                    # G.k.2.3.r.1
                                    set_icsr_field(self.find(self.find(ingredient_new, "ingredientSubstance"), "name"), None, substance, "g_k_2_3_r_1_substance_name")
                                    kindOfProduct.append(ingredient_new)
                                subjectOfIngredient = self.find(instanceOfKind, "subjectOf")
                                performer = self.find(self.find(subjectOfIngredient, "productEvent"), "performer")
                                country = self.find(self.find(self.find(self.find(performer, "assignedEntity"), "representedOrganization"), "addr"), "country")
                                # G.k.2.4
                                set_icsr_field(country, None, drug_info, "g_k_2_4_identification_country_drug_obtained")
                                outboundRelationship1s = self.find(substanceAdministration, "outboundRelationship1")
                                for i, outboundRelationship1 in enumerate(outboundRelationship1s, start=1):
                                    substanceAdministration.remove(outboundRelationship1)
                                    for reaction_matrix in drug_info.g_k_9_i_drug_reaction_matrix:
                                        outboundRelationship1Copy = copy.deepcopy(outboundRelationship1)
                                        pauseQuantity = self.find(outboundRelationship1Copy, "pauseQuantity")
                                        self.find(self.find(outboundRelationship1Copy, "actReference"), "id").set("root", f"r-id{i}")
                                        # no other identifiers
                                        if pauseQuantity.get("unit") == "G.k.9.i.3.1b":
                                            # G.k.9.i.3.1a
                                            set_icsr_field(pauseQuantity, "value", reaction_matrix, "g_k_9_i_3_1a_interval_drug_administration_reaction_num")
                                            # G.k.9.i.3.1b
                                            set_icsr_field(pauseQuantity, "unit", reaction_matrix, "g_k_9_i_3_1b_interval_drug_administration_reaction_unit")
                                        elif pauseQuantity.get("unit") == "G.k.9.i.3.1b":
                                            # G.k.9.i.3.2a
                                            set_icsr_field(pauseQuantity, "value", reaction_matrix, "g_k_9_i_3_2a_interval_last_dose_drug_reaction_num")
                                            # G.k.9.i.3.2b
                                            set_icsr_field(pauseQuantity, "unit", reaction_matrix, "g_k_9_i_3_2b_interval_last_dose_drug_reaction_unit")
                                        substanceAdministration.append(outboundRelationship1Copy)

                                outboundRelationship2s = self.find(substanceAdministration, "outboundRelationship2")
                                for outboundRelationship2 in outboundRelationship2s:
                                    typeCode = outboundRelationship2.get("typeCode")
                                    if typeCode == "PERT" or typeCode == "SUMM" or typeCode == "REFR":
                                        observationTmp = self.find(outboundRelationship2, "observation")
                                        observationCode = self.find(observationTmp, "code").get("code")
                                        valTmp = self.find(observationTmp, "value")
                                        if observationCode == "6":
                                            # G.k.2.5
                                            set_icsr_field(valTmp, "value", drug_info, "g_k_2_5_investigational_product_blinded")
                                        elif observationCode == "14":
                                            # G.k.5a
                                            set_icsr_field(valTmp, "value", drug_info, "g_k_5a_cumulative_dose_first_reaction_num")
                                            # G.k.5b
                                            set_icsr_field(valTmp, "unit", drug_info, "g_k_5b_cumulative_dose_first_reaction_unit")
                                        elif observationCode == "16":
                                            # G.k.6a
                                            set_icsr_field(valTmp, "value", drug_info, "g_k_6a_gestation_period_exposure_num")
                                            # G.k.6b
                                            set_icsr_field(valTmp, "unit", drug_info, "g_k_6b_gestation_period_exposure_unit")
                                        elif observationCode == "31":   
                                            substanceAdministration.remove(outboundRelationship2)
                                            for i, react_matrix in enumerate(drug_info.g_k_9_i_drug_reaction_matrix, start=1):
                                                outboundRelationship2_new = copy.deepcopy(outboundRelationship2)
                                                observationOutboundRelationship2 = self.find(outboundRelationship2_new, "observation")
                                                # G.k.9.i.4
                                                set_icsr_field(self.find(observationOutboundRelationship2, "value"), "code", react_matrix, "g_k_9_i_4_reaction_recur_readministration")
                                                self.find(
                                                    self.find(self.find(observationOutboundRelationship2, "outboundRelationship1"), "actReference"),
                                                    "id"
                                                ).set("root", f'r-id{i}')
                                                substanceAdministration.append(outboundRelationship2_new)
                                        elif observationCode == "9":
                                            substanceAdministration.remove(outboundRelationship2)
                                            for i, additional_information in enumerate(drug_info.g_k_10_r_additional_information_drug, start=1):
                                                outboundRelationship2_new = copy.deepcopy(outboundRelationship2)
                                                observationTmp = self.find(outboundRelationship2_new, "observation")
                                                observationCode = self.find(observationTmp, "code").get("code")
                                                valTmp = self.find(observationTmp, "value")
                                                # G.k.10.r
                                                set_icsr_field(valTmp, "code", additional_information, "g_k_10_r_additional_information_drug")
                                                substanceAdministration.append(outboundRelationship2_new)
                                        elif observationCode == "2":
                                            # G.k.11
                                            set_icsr_field(valTmp, None, drug_info, "g_k_11_additional_information_drug")
                                    elif typeCode == "COMP":
                                        substanceAdministration.remove(outboundRelationship2)
                                        for dosage_information in drug_info.g_k_4_r_dosage_information:
                                            outboundRelationship2_new = copy.deepcopy(outboundRelationship2)
                                            substanceAdministration_new = self.find(outboundRelationship2_new, "substanceAdministration")
                                            # G.k.4.r.8
                                            set_icsr_field(self.find(substanceAdministration_new, "text"), None, dosage_information, "g_k_4_r_8_dosage_text")
                                            effectiveTime = self.find(substanceAdministration_new, "effectiveTime")
                                            comps = self.find(effectiveTime, "comp")
                                            for comp in comps:
                                                period = self.find(comp, "period")
                                                low = self.find(comp, "low")
                                                high = self.find(comp, "high")
                                                width = self.find(comp, "width")
                                                if period is not None:
                                                    # G.k.4.r.2
                                                    set_icsr_field(period, "value", dosage_information, "g_k_4_r_2_number_units_interval")
                                                    # G.k.4.r.3
                                                    set_icsr_field(period, "unit", dosage_information, "g_k_4_r_3_definition_interval_unit")
                                                elif low is not None:
                                                    # G.k.4.r.4
                                                    set_text_icsr_field_with_null(low, "value", dosage_information, "g_k_4_r_4_date_time_drug")
                                                    # G.k.4.r.5
                                                    set_text_icsr_field_with_null(high, "value", dosage_information, "g_k_4_r_5_date_time_last_administration")
                                                elif width is not None:
                                                    # G.k.4.r.6a
                                                    set_icsr_field(width, "value", dosage_information, "g_k_4_r_6a_duration_drug_administration_num")
                                                    # G.k.4.r.6b
                                                    set_icsr_field(width, "unit", dosage_information, "g_k_4_r_6b_duration_drug_administration_unit")
                                            
                                            # G.k.4.r.10.2a
                                            set_icsr_field(self.find(substanceAdministration_new, "routeCode"), "codeSystemVersion", dosage_information, "g_k_4_r_10_2a_route_administration_termid_version")
                                            # G.k.4.r.10.2b
                                            set_icsr_field(self.find(substanceAdministration_new, "routeCode"), "code", dosage_information, "g_k_4_r_10_2b_route_administration_termid")
                                            # G.k.4.r.10.1
                                            set_text_icsr_field_with_null(self.find(self.find(substanceAdministration_new, "routeCode"), "originalText"), None, dosage_information, "g_k_4_r_10_1_route_administration")
                                                                                        
                                            doseQuantity = self.find(substanceAdministration_new, "doseQuantity")
                                            # G.k.4.r.1a
                                            set_icsr_field(doseQuantity, "value", dosage_information, "g_k_4_r_1a_dose_num")
                                            # G.k.4.r.1b
                                            set_icsr_field(doseQuantity, "unit", dosage_information, "g_k_4_r_1b_dose_unit")
                                            instanceOfKind = self.find(self.find(substanceAdministration_new, "consumable"), "instanceOfKind")
                                            productInstanceInstance = self.find(instanceOfKind, "productInstanceInstance")
                                            # G.k.4.r.7
                                            set_icsr_field(self.find(productInstanceInstance, "lotNumberText"), None, dosage_information, "g_k_4_r_7_batch_lot_number")
                                            kindOfProduct = self.find(instanceOfKind, "kindOfProduct")
                                            formCode = self.find(kindOfProduct, "formCode")
                                            # G.k.4.r.9.2a
                                            set_icsr_field(formCode, "codeSystemVersion", dosage_information, "g_k_4_r_9_2a_pharmaceutical_dose_form_termid_version")
                                            # G.k.4.r.9.2b
                                            set_icsr_field(formCode, "codeSystem", dosage_information, "g_k_4_r_9_2b_pharmaceutical_dose_form_termid")
                                            # G.k.4.r.9.1
                                            set_text_icsr_field_with_null(self.find(formCode, "originalText"), None, dosage_information, "g_k_4_r_9_1_pharmaceutical_dose_form")
                                            valueInboundRelationshipTmp = self.find(self.find(self.find(substanceAdministration_new, "inboundRelationship"), "observation"), "value")
                                            # G.k.4.r.11.2a
                                            set_icsr_field(valueInboundRelationshipTmp, "codeSystemVersion", dosage_information, "g_k_4_r_11_2a_parent_route_administration_termid_version")
                                            # G.k.4.r.11.2b
                                            set_icsr_field(formCode, "code", dosage_information, "g_k_4_r_11_2b_parent_route_administration_termid")
                                            # G.k.4.r.11.1
                                            set_text_icsr_field_with_null(self.find(valueInboundRelationshipTmp, "originalText"), None, dosage_information, "g_k_4_r_11_1_parent_route_administration")
                                             
                                            substanceAdministration.append(outboundRelationship2_new)
                                        
                                inboundRelationships = self.find(substanceAdministration, "inboundRelationship")
                                for inboundRelationship in inboundRelationships:
                                    typeCode = inboundRelationship.get("typeCode")
                                    if typeCode == "RSON":
                                        observationInboundRelationship = self.find(inboundRelationship, "observation")
                                        val = self.find(observationInboundRelationship, "value")
                                        observationInboundRelationship.remove(val)
                                        for indication in drug_info.g_k_7_r_indication_use_case:
                                            val_new = copy.deepcopy(val)
                                            # G.k.7.r.2a
                                            set_icsr_field(val_new, "codeSystemVersion", indication, "g_k_7_r_2a_meddra_version_indication")
                                            # G.k.7.r.2b
                                            set_icsr_field(val_new, "code", indication, "g_k_7_r_2b_indication_meddra_code")
                                            # G.k.7.r.1
                                            set_text_icsr_field_with_null(self.find(val_new, "originalText"), None, indication, "g_k_7_r_1_indication_primary_source")
                                            observationInboundRelationship.append(val_new)
                                    elif typeCode == "CAUS":
                                        # G.k.8
                                        set_icsr_field(
                                            self.find(self.find(inboundRelationship, "act"), "code"),
                                            "code",
                                            drug_info,
                                            "g_k_8_action_taken_drug"
                                        )
                                organizer.append(component_new)
                # end of subject1
        
                components = self.find(adverseEventAssessment, "component")
                for component in components:
                    causalityAssessment = self.find(component, "causalityAssessment")
                    codeCausalityAssessment = self.find(causalityAssessment, "code").get("code")
                    adverseEventAssessment.remove(component)
                    for k, drug_info in enumerate(gk, start=1):
                        if codeCausalityAssessment == "20":
                            component_new = copy.deepcopy(component)
                            causalityAssessment_new = self.find(component_new, "causalityAssessment")
                            # G.k.1
                            set_icsr_field(
                                self.find(causalityAssessment_new, "value"), 
                                "code", 
                                drug_info, 
                                "g_k_1_characterisation_drug_role"
                            )
                            self.find(self.find(self.find(causalityAssessment_new, "subject2"), "productUseReference"), "id").set("root", f'd-id{k}')
                            adverseEventAssessment.append(component_new)
                        elif codeCausalityAssessment == "39":
                            for i, react_matrix in enumerate(drug_info.g_k_9_i_drug_reaction_matrix, start=1):
                                for r, relatedness_drug_reaction in enumerate(react_matrix.g_k_9_i_2_r_assessment_relatedness_drug_reaction, start=1):
                                    component_new = copy.deepcopy(component)
                                    causalityAssessment_new = self.find(component_new, "causalityAssessment")
                                    # G.k.9.i.2.r.3
                                    set_icsr_field(self.find(causalityAssessment_new, "value"), None, relatedness_drug_reaction, "g_k_9_i_2_r_3_result_assessment")
                                    # G.k.9.i.2.r.2
                                    set_icsr_field(
                                        self.find(self.find(causalityAssessment_new, "methodCode"), "originalText"),
                                        None, 
                                        relatedness_drug_reaction,
                                        "g_k_9_i_2_r_2_method_assessment"
                                    )
                                    # G.k.9.i.2.r.1
                                    set_icsr_field(
                                        self.find(self.find(
                                            self.find(self.find(causalityAssessment_new, "author"), "assignedEntity"),
                                            "code"
                                        ), "originalText"),
                                        None,
                                        relatedness_drug_reaction,
                                        "g_k_9_i_2_r_1_source_assessment"
                                    )
                                    self.find(self.find(self.find(causalityAssessment_new, "subject1"), "adverseEffectReference"), "id").set("root", f'r-id{k}')
                                    self.find(self.find(self.find(causalityAssessment_new, "subject2"), "productUseReference"), "id").set("root", f'd-id{i}')                                            
                                    adverseEventAssessment.append(component_new)
                
                components1 = self.find(adverseEventAssessment, "component1")
                for component1 in components1:
                    codeTmp = self.find(self.find(component1, "observationEvent"), "code").get("code")
                    if codeTmp == "10":
                        codeLocal = self.find(self.find(self.find(self.find(component1, "observationEvent"), "author"), "assignedEntity"), "code").get("code")
                        if codeLocal == "3":
                            # H.2
                            set_icsr_field(
                                self.find(self.find(component1, "observationEvent"), "value"),
                                None,
                                h,
                                "h_2_reporter_comments"
                            )
                        elif codeLocal == "1":
                            # H.4
                            set_icsr_field(
                                self.find(self.find(component1, "observationEvent"), "value"),
                                None,
                                h,
                                "h_4_sender_comments"
                            )
                    elif codeTmp == "15":
                        adverseEventAssessment.remove(component1)
                        for diagnosis_meddra_code in h.h_3_r_sender_diagnosis_meddra_code:
                            component1Copy = copy.deepcopy(component1)
                            valTmp = self.find(self.find(component1Copy, "observationEvent"), "value")
                            # H.3.r.1a
                            set_icsr_field(valTmp, "codeSystemVersion", diagnosis_meddra_code, "h_3_r_1a_meddra_version_sender_diagnosis")
                            # H.3.r.1b
                            set_icsr_field(valTmp, "code", diagnosis_meddra_code, "h_3_r_1b_sender_diagnosis_meddra_code")
                            adverseEventAssessment.append(component1Copy)
                # end of adverseEventAssessment

            elif observationEvent is not None:
                codeObservationEvent = self.find(observationEvent, "code").get("code")
                if codeObservationEvent == "1":
                    # C.1.6.1
                    set_icsr_field(self.find(observationEvent, "value"), "value", c1, "c_1_6_1_additional_documents_available")
                if codeObservationEvent == "23":
                    # C.1.7
                    set_text_icsr_field_with_null(self.find(observationEvent, "value"), "value", c1, "c_1_7_fulfil_local_criteria_expedited_report")
                if codeObservationEvent == "36":
                    investigationEvent.remove(component_big)
                    if h is not None:
                        for case_summary in h.h_5_r_case_summary_reporter_comments_native_language:
                            component_new = copy.deepcopy(component_big)
                            observationEvent_new = self.find(component_new, "observationEvent")
                            val = self.find(observationEvent_new, "value")
                            # H.5.r.1a
                            set_icsr_field(val, None, case_summary, "h_5_r_1a_case_summary_reporter_comments_text")
                            # H.5.r.1b
                            set_icsr_field(val, "language", case_summary, "h_5_r_1b_case_summary_reporter_comments_language")                            
                            investigationEvent.append(component_new)
                    
        outboundRelationships = self.find(investigationEvent, 'outboundRelationship')
        for outboundRelationship in outboundRelationships:
            relatedInvestigation = self.find(outboundRelationship, "relatedInvestigation")
            codeRelatedInvestigation = self.find(relatedInvestigation, "code")
            if codeRelatedInvestigation.get("code") == "1":
                assignedEntitySmall = self.find(self.find(self.find(self.find(relatedInvestigation, "subjectOf2"), "controlActEvent"), "author"), "assignedEntity")
                # C.1.8.2
                set_icsr_field(self.find(assignedEntitySmall, "code"), "code", c1, "c_1_8_2_first_sender")
            elif codeRelatedInvestigation.get("nullFlavor") == "NA":
                investigationEvent.remove(outboundRelationship)
                if c1 is not None:
                    for identification_number_report_linked in c1.c_1_10_r_identification_number_report_linked:
                        outboundRelationshipCopy = copy.deepcopy(outboundRelationship)
                        idSmall = self.find(self.find(self.find(self.find(outboundRelationshipCopy, "relatedInvestigation"), "subjectOf2"), "controlActEvent"), "id")
                        # C.1.10.r
                        set_icsr_field(idSmall, "extension", identification_number_report_linked, "c_1_10_r_identification_number_report_linked")
                        investigationEvent.append(outboundRelationshipCopy)
            elif codeRelatedInvestigation.get("code") == "2":
                investigationEvent.remove(outboundRelationship)
                for primary_source in c2:
                    outboundRelationshipCopy = copy.deepcopy(outboundRelationship)
                    # C.2.r.5
                    set_icsr_field(self.find(outboundRelationshipCopy, "priorityNumber"), "value", primary_source, "c_2_r_5_primary_source_regulatory_purposes")
                    assignedEntitySmall = self.find(self.find(self.find(self.find(
                        self.find(outboundRelationshipCopy, "relatedInvestigation"),
                        "subjectOf2"),
                        "controlActEvent"),
                        "author"),
                        "assignedEntity"
                    )
                    addr = self.find(assignedEntitySmall, "addr")
                    # C.2.r.2.3
                    set_text_icsr_field_with_null(self.find(addr, "streetAddressLine"), None, primary_source, "c_2_r_2_3_reporter_street")
                    # C.2.r.2.4
                    set_text_icsr_field_with_null(self.find(addr, "city"), None, primary_source, "c_2_r_2_4_reporter_city")
                    # C.2.r.2.5
                    set_text_icsr_field_with_null(self.find(addr, "state"), None, primary_source, "c_2_r_2_5_reporter_state_province")
                    # C.2.r.2.6
                    set_text_icsr_field_with_null(self.find(addr, "postalCode"), None, primary_source, "c_2_r_2_6_reporter_postcode")
                    # C.2.r.2.7
                    set_text_icsr_field_with_null(self.find(assignedEntitySmall, "telecom"), "value", primary_source, "c_2_r_2_7_reporter_telephone", get_value=lambda x: f'tel:{str(x.value)}')
                    assignedPerson = self.find(assignedEntitySmall, "assignedPerson")
                    name = self.find(assignedPerson, "name")
                    # C.2.r.1.1
                    set_text_icsr_field_with_null(self.find(name, "prefix"), None, primary_source, "c_2_r_1_1_reporter_title")
                    givens = self.find(name, "given")
                    for given, field in zip(givens, ["c_2_r_1_2_reporter_given_name", "c_2_r_1_3_reporter_middle_name"]):
                        # C.2.r.1.2 / C.2.r.1.3
                        set_icsr_field(given, None, primary_source, field)
                    # C.2.r.1.4
                    set_text_icsr_field_with_null(self.find(name, "family"), None, primary_source, "c_2_r_1_4_reporter_family_name")
                    # C.2.r.4
                    set_text_icsr_field_with_null(
                        self.find(self.find(assignedPerson, "asQualifiedEntity"), "code"),
                        "code",
                        primary_source,
                        "c_2_r_4_qualification"
                    )
                    # C.2.r.3
                    set_text_icsr_field_with_null(
                        self.find(self.find(self.find(assignedPerson, "asLocatedEntity"), "location"), "code"),
                        "code",
                        primary_source,
                        "c_2_r_3_reporter_country_code"
                    )
                    representedOrganization = self.find(assignedEntitySmall, "representedOrganization")
                    # C.2.r.2.2
                    set_text_icsr_field_with_null(self.find(representedOrganization, "name"), None, primary_source, "c_2_r_2_2_reporter_department")
                    # C.2.r.2.1
                    set_text_icsr_field_with_null(
                        self.find(self.find(self.find(representedOrganization, "assignedEntity"), "representedOrganization"), "name"),
                        None, 
                        primary_source,
                        "c_2_r_2_1_reporter_organisation"
                    )
                    investigationEvent.append(outboundRelationshipCopy)                          
        
        subjectOf1s = self.find(investigationEvent, 'subjectOf1')
        for subjectOf1 in subjectOf1s:
            controlActEventLocall = self.find(subjectOf1, "controlActEvent")
            idControlActEventLocall = self.find(controlActEventLocall, "id")
            author = self.find(controlActEventLocall, "author")
            if idControlActEventLocall is not None:
                investigationEvent.remove(subjectOf1)
                if c1 is not None:
                    for documents_held_sender in c1.c_1_9_1_r_source_case_id:
                        subjectOf1Copy = copy.deepcopy(subjectOf1)
                        idControlActEventLocallCopy = self.find(self.find(subjectOf1Copy, "controlActEvent"), "id")
                        # C.1.9.1.r.1
                        set_icsr_field(idControlActEventLocallCopy, "assigningAuthorityName", documents_held_sender, "c_1_9_1_r_1_source_case_id")
                        # C.1.9.1.r.2
                        set_icsr_field(idControlActEventLocallCopy, "extension", documents_held_sender, "c_1_9_1_r_2_case_id")
                        investigationEvent.append(subjectOf1Copy)
            elif author is not None:
                assignedEntitySmall = self.find(author, "assignedEntity")
                # C.3.1
                set_icsr_field(self.find(assignedEntitySmall, "code"), "code", c3, "c_3_1_sender_type")
                addr = self.find(assignedEntitySmall, "addr")
                # C.3.4.1
                set_icsr_field(self.find(addr, "streetAddressLine"), None, c3, "c_3_4_1_sender_street_address")
                # C.3.4.2
                set_icsr_field(self.find(addr, "city"), None, c3, "c_3_4_2_sender_city")
                # C.3.4.3
                set_icsr_field(self.find(addr, "state"), None, c3, "c_3_4_3_sender_state_province")
                # C.3.4.4
                set_icsr_field(self.find(addr, "postalCode"), None, c3, "c_3_4_4_sender_postcode")
                telecoms = self.find(assignedEntitySmall, "telecom")
                for telecom, pair in zip(telecoms, [
                    ("c_3_4_6_sender_telephone", "tel"),
                    ("c_3_4_7_sender_fax", "fax"),
                    ("c_3_4_8_sender_email", "mailto")
                ]):
                    field, prefix = pair
                    # C.3.4.6 / C.3.4.7 / C.3.4.8
                    set_icsr_field(telecom, "value", c3, field, get_value=lambda x: f'{prefix}:{str(x.value)}' )
                assignedPerson = self.find(assignedEntitySmall, "assignedPerson")
                name = self.find(assignedPerson, "name")
                # C.3.3.2
                set_icsr_field(self.find(name, "prefix"), None, c3, "c_3_3_2_sender_title")
                givens = self.find(name, "given")
                for given, field in zip(givens, ["c_3_3_3_sender_given_name", "c_3_3_4_sender_middle_name"]):
                    # C.3.3.3 / C.3.3.4
                    set_icsr_field(given, None, c3, field)
                # C.3.3.5
                set_icsr_field(self.find(name, "family"), None, c3, "c_3_3_5_sender_family_name")
                # 3.4.5
                set_icsr_field(
                    self.find(self.find(self.find(assignedPerson, "asLocatedEntity"), "location"), "code"),
                    "code",
                    c3,
                    "c_3_4_5_sender_country_code"
                )
                representedOrganization = self.find(assignedEntitySmall, "representedOrganization")
                # C.3.3.1
                set_icsr_field(self.find(representedOrganization, "name"), None, c3, "c_3_3_1_sender_department")
                # C.3.2
                set_icsr_field(
                    self.find(self.find(self.find(representedOrganization, "assignedEntity"), "representedOrganization"), "name"),
                    None, 
                    c3,
                    "c_3_2_sender_organisation"
                )

        vars = self.find(investigationEvent, 'subjectOf2')
        for var in vars:
            var = self.find(var, 'investigationCharacteristic')
            code = self.find(var, "code").get("code")
            tmp_var = self.find(var,'value')
            if code == "1":
                # C.1.3
                set_icsr_field(tmp_var, "code", c1, "c_1_3_type_report")
            elif code == "2":
                # C.1.9.1
                set_text_icsr_field_with_null(tmp_var, None, c1, "c_1_9_1_other_case_ids_previous_transmissions")
            elif code == "3":
                # C.1.11.1
                set_icsr_field(tmp_var, "code", c1, "c_1_11_1_report_nullification_amendment")
            elif code == "4":
                # C.1.11.2
                set_text_icsr_field_with_null(self.find(tmp_var, "originalText"), None, c1, "c_1_11_2_reason_nullification_amendment")
        return root                       

    
    @classmethod
    def extend_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for key, value in model_dict.items():
            if isinstance(value, dict):
                cls.extend_lists(value)
            if isinstance(value, list) and len(value) == 1:
                value.append(dict())


class ImportMultipleXmlView(BaseView):
    @log
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        # try:
        files_data = json.loads(request.body)
        xml_contents = files_data.get('files', [])
        results = []

        for idx, xml_content in enumerate(xml_contents):
            try:
                root = etree.fromstring(xml_content.encode('utf-8'))
                
                porr_elements = self.find(root, "PORR_IN049016UV")
                
                if porr_elements is None:
                    results.append({
                        "success": False,
                        "filename": f"file_{idx}",
                        "error": "PORR_IN049016UV element not found"
                    })
                    continue
                
                for i, porr_elem in enumerate((porr_elements if isinstance(porr_elements, list) else [porr_elements]), start=1):
                        icsr = ICSR()
                        icsr = self.import_single_xml(porr_elem, icsr)
                        icsr, status = self.domain_service.create(icsr)
                        
                        if not status:
                            raise Exception(json.dumps(icsr.errors, indent=2))
                        results.append({
                            "success": True,
                            "filename": f"file_{idx}",
                            "icsr_result_id": icsr.id
                        })
                        response_data = {
                            "results": results,
                            "total": len(xml_contents),
                            "successful": sum(1 for r in results if r.get("success", False)),
                            "failed": sum(1 for r in results if not r.get("success", False))
                        }
                    
                        return self.respond_with_object_as_json(response_data, HTTPStatus.OK)
            except Exception as e:
                traceback.print_exc()
                print(f"Error in ImportMultipleXmlView: {str(e)}")
                return http.HttpResponse(f'Error processing request: {str(e)}', 
                                        status=HTTPStatus.INTERNAL_SERVER_ERROR)

    @staticmethod
    def find(root, field, pretty=True):
        ns = {
            'hl7': "urn:hl7-org:v3",
        }
        res = root.findall(f'hl7:{field}', ns)
        if len(res) == 1 and pretty:
            return res[0]
        elif len(res) >= 1:
            return res 

        
    @staticmethod
    def set_id_sender_receiver_creation_time(root):
        ImportMultipleXmlView.find(root, 'id').set('extension', str(uuid.uuid4()))

        for agent in ['sender', 'receiver']:
            agent_xml = ImportMultipleXmlView.find(root, agent)
            device = ImportMultipleXmlView.find(agent_xml, 'device')
            id_element = ImportMultipleXmlView.find(device, 'id')
            id_element.set('extension', str(uuid.uuid4()))

        ImportMultipleXmlView.find(root, 'creationTime').set("value", datetime.now().strftime('%Y%m%d%H%M%S'))

    def import_single_xml(self, root, icsr):
        def set_icsr_field(root, key, obj, field, get_value=lambda x: x):
            if root is None:
                return
            value = None
            if key is not None and root.get(key) is not None:
                value = root.get(key)
            elif root.text is not None:
                value = root.text.strip()
            if value is not None:
                field_type = get_type_annotation(obj.__class__, field)
                converted_value = convert_to_type(get_value(value), field_type, field)
                setattr(obj, field, converted_value)

        def set_text_icsr_field_with_null(root, key, obj, field, get_value=lambda x: x):
            if root is not None and root.get("nullFlavor") is not None:
                null_flavor_value = root.get("nullFlavor")
                null_flavor = getattr(NF, null_flavor_value)
                setattr(obj, field, null_flavor)
            else:
                set_icsr_field(root, key, obj, field, get_value)

        def get_type_annotation(cls, field_name):
            if not hasattr(cls, 'model_fields'):
                return None
            
            field_info = cls.model_fields.get(field_name)
            if field_info is None:
                return None
            
            return field_info.annotation
            
        def convert_to_type(value, target_type, field=None):
            easy = ["int", "str"]
            name = target_type.__name__
            if name.startswith("NullableValue"):
                # No need to process NF
                name, _ = name[len("NullableValue["):].split(',', 1)
            elif name.startswith("Value"):
                name = name[len("Value["):-1]
            if name.startswith("Literal"):
                name = name[len("Literal["):-1]
                if name == "True":
                    return True

            if name in easy:
                return getattr(builtins, name)(value)
            elif name.lower() == "decimal":
                return decimal.Decimal(value)
            elif name.lower() == "bool":
                if value.lower() == "true":
                    return True
                elif value.lower() == "false":
                    return False
                else:
                    raise TypeError(f"{field} must have bool type, {value} given")
            else:
                emun_class = getattr(enums, name)
                if issubclass(emun_class, enum.IntEnum):
                    return emun_class(int(value))
                if issubclass(emun_class, enum.StrEnum):
                    return emun_class(value)
            return value

        c1 = icsr.c_1_identification_case_safety_report
        c2 = icsr.c_2_r_primary_source_information
        c3 = icsr.c_3_information_sender_case_safety_report
        c4 = icsr.c_4_r_literature_reference
        c5 = icsr.c_5_study_identification
        d = icsr.d_patient_characteristics
        ei = icsr.e_i_reaction_event
        fr = icsr.f_r_results_tests_procedures_investigation_patient
        gk = icsr.g_k_drug_information
        h = icsr.h_narrative_case_summary

        if icsr.c_1_identification_case_safety_report is None:
            icsr.c_1_identification_case_safety_report = c1 = C_1_identification_case_safety_report(icsr=icsr)
        if icsr.c_3_information_sender_case_safety_report is None:
            icsr.c_3_information_sender_case_safety_report = c3 = C_3_information_sender_case_safety_report(icsr=icsr)
        if icsr.c_5_study_identification is None:
            icsr.c_5_study_identification = c5 = C_5_study_identification(icsr=icsr)
        if icsr.d_patient_characteristics is None:
            icsr.d_patient_characteristics = d = D_patient_characteristics(icsr=icsr)
        if icsr.h_narrative_case_summary is None:
            icsr.h_narrative_case_summary = h = H_narrative_case_summary(icsr=icsr)

        control_act_process = self.find(root, "controlActProcess")
        # C.1.2
        set_icsr_field(self.find(control_act_process, "effectiveTime"), 'value', c1, "c_1_2_date_creation")

        investigationEvent = self.find(self.find(control_act_process, "subject"), "investigationEvent")
        # C.1.1
        set_icsr_field(self.find(investigationEvent, 'id[@root="2.16.840.1.113883.3.989.2.1.3.1"]'), "extension", c1, "c_1_1_sender_safety_report_unique_id")

        # C.1.8.1
        set_icsr_field(self.find(investigationEvent, 'id[@root="2.16.840.1.113883.3.989.2.1.3.2"]'), "extension", c1, "c_1_8_1_worldwide_unique_case_identification_number")

        # H.1
        set_icsr_field(self.find(investigationEvent, 'text'), None, h, "h_1_case_narrative")

        # C.1.4
        set_icsr_field(
            self.find(self.find(investigationEvent, "effectiveTime"), "low"),
            "value",
            c1,
            "c_1_4_date_report_first_received_source"
        )

        # C.1.5 
        set_icsr_field(
            self.find(investigationEvent, "availabilityTime"),
            "value",
            c1,
            "c_1_5_date_most_recent_information"
        )

        references = self.find(investigationEvent, "reference")
        for reference in references:
            document = self.find(reference, "document")
            codeDocument = self.find(document, "code").get("code")
            if codeDocument == "1":
                documents_held_sender = C_1_6_1_r_documents_held_sender(c_1_identification_case_safety_report=c1)
                title = self.find(self.find(reference, "document"), "title")
                # C.1.6.1.r.1
                set_icsr_field(title, None, documents_held_sender, "c_1_6_1_r_1_documents_held_sender")
                c1.c_1_6_1_r_documents_held_sender.append(documents_held_sender)
            elif codeDocument == "2":
                literature_reference = C_4_r_literature_reference(icsr=icsr)
                bibliographicDesignationText = self.find(self.find(reference, "document"), "bibliographicDesignationText")
                # C.4.r.1
                set_text_icsr_field_with_null(bibliographicDesignationText, None, literature_reference, "c_4_r_1_literature_reference")
                c4.append(literature_reference)

            
        components_big = self.find(investigationEvent, 'component')
        for component_big in components_big:
            adverseEventAssessment = self.find(component_big, "adverseEventAssessment")
            observationEvent = self.find(component_big, "observationEvent")
            if adverseEventAssessment is not None:
                primaryRole = self.find(self.find(adverseEventAssessment, "subject1"), "primaryRole")
                player1 = self.find(primaryRole, "player1")
                
                # D.1
                set_text_icsr_field_with_null(self.find(player1, "name"), None, d, "d_1_patient")
                
                # D.5 
                set_text_icsr_field_with_null(self.find(player1, "administrativeGenderCode"), "code", d, "d_5_sex")

                # D.2.1
                set_text_icsr_field_with_null(self.find(player1, "birthTime"), "value", d, "d_2_1_date_birth")

                # D.9.1
                set_text_icsr_field_with_null(self.find(player1, "deceasedTime"), "value", d, "d_9_1_date_death")

                asIdentifiedEntitys = self.find(player1, "asIdentifiedEntity")
                for asIdentifiedEntity in asIdentifiedEntitys:
                    codeTmp = self.find(asIdentifiedEntity, "code").get("code")
                    if codeTmp == "1":
                        # D.1.1.1
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_1_medical_record_number_source_gp"
                        )
                    elif codeTmp == "2":
                        # D.1.1.2
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_2_medical_record_number_source_specialist"
                        )
                    elif codeTmp == "3":
                        # D.1.1.3
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_3_medical_record_number_source_hospital"
                        )
                    elif codeTmp == "4":
                        # D.1.1.4
                        set_text_icsr_field_with_null(
                            self.find(asIdentifiedEntity, "id"), 
                            "extension",
                            d, 
                            "d_1_1_4_medical_record_number_source_investigation"
                        )


                role = self.find(player1, "role")
                associatedPersonRole = self.find(role, "associatedPerson")
                # D.10.1
                set_text_icsr_field_with_null(self.find(associatedPersonRole, "name"), None, d, "d_10_1_parent_identification")
                # D.10.6
                set_text_icsr_field_with_null(self.find(associatedPersonRole, "administrativeGenderCode"), "code", d, "d_10_6_sex_parent")
                # D.10.2.1
                set_text_icsr_field_with_null(self.find(associatedPersonRole, "birthTime"), "value", d, "d_10_2_1_date_birth_parent")
            
                subjectOf2Roles = self.find(role, "subjectOf2")
                for subjectOf2Role in subjectOf2Roles:
                    observationSubjectOf2 = self.find(subjectOf2Role, "observation")
                    organizerSubjectOf2 = self.find(subjectOf2Role, "organizer")
                    if observationSubjectOf2 is not None:
                        codeObservationSubjectOf2 = self.find(observationSubjectOf2, "code").get("code")
                        if codeObservationSubjectOf2 == "3":
                            # D.10.2.2a
                            set_icsr_field(self.find(observationSubjectOf2, "value"), "value", d, "d_10_2_2a_age_parent_num")
                            # D.10.2.2b
                            set_icsr_field(self.find(observationSubjectOf2, "value"), "unit", d, "d_10_2_2b_age_parent_unit")
                        elif codeObservationSubjectOf2 == "22":
                            # D.10.3
                            set_text_icsr_field_with_null(self.find(observationSubjectOf2, "value"), "value", d, "d_10_3_last_menstrual_period_date_parent")
                        elif codeObservationSubjectOf2 == "7":
                            # D.10.4
                            set_text_icsr_field_with_null(self.find(observationSubjectOf2, "value"), "value", d, "d_10_4_body_weight_parent")
                        elif codeObservationSubjectOf2 == "17":
                            # D.10.5
                            set_text_icsr_field_with_null(self.find(observationSubjectOf2, "value"), "value", d, "d_10_5_height_parent")
                    elif organizerSubjectOf2 is not None:
                        codeOrganizerSubjectOf2 = self.find(organizerSubjectOf2, "code").get("code")
                        if codeOrganizerSubjectOf2 == "1":
                            componentsSmall = self.find(organizerSubjectOf2, "component", False)
                            for componentSmall in componentsSmall:
                                observationLocal = self.find(componentSmall, "observation")
                                codeSystemObservationLocal = self.find(observationLocal, "code").get("codeSystem")
                                if codeSystemObservationLocal == "2.16.840.1.113883.6.163":
                                    parent_info = D_10_7_1_r_structured_information_parent_meddra_code(d_patient_characteristics=d)
                                    # D.10.7.1.r.1a
                                    set_icsr_field(self.find(observationLocal, "code"), "codeSystemVersion", parent_info, "d_10_7_1_r_1a_meddra_version_medical_history")
                                    # D.10.7.1.r.1b
                                    set_icsr_field(self.find(observationLocal, "code"), "code", parent_info, "d_10_7_1_r_1b_medical_history_meddra_code")
                                    effectiveTimeLocalSmaller = self.find(observationLocal, "effectiveTime")
                                    # D.10.7.1.r.2
                                    set_text_icsr_field_with_null(self.find(effectiveTimeLocalSmaller, "low"), "value", parent_info, "d_10_7_1_r_2_start_date")
                                    # D.10.7.1.r.4
                                    set_text_icsr_field_with_null(self.find(effectiveTimeLocalSmaller, "high"), "value", parent_info, "d_10_7_1_r_4_end_date")
                                    outboundRelationship2LocalSmaller = self.find(observationLocal, "outboundRelationship2")
                                    observationOutboundRelationship2LocalSmaller = self.find(outboundRelationship2LocalSmaller, "observation")
                                    # D.10.7.1.r.5
                                    set_icsr_field(self.find(observationOutboundRelationship2LocalSmaller, "value"), None, parent_info, "d_10_7_1_r_5_comments")
                                    inboundRelationship2LocalSmaller = self.find(observationLocal, "inboundRelationship")
                                    observationOutboundRelationship2LocalSmaller = self.find(inboundRelationship2LocalSmaller, "observation")
                                    # D.10.7.1.r.3
                                    set_text_icsr_field_with_null(self.find(observationOutboundRelationship2LocalSmaller, "value"), "value", parent_info, "d_10_7_1_r_3_continuing")
                                    d.d_10_7_1_r_structured_information_parent_meddra_code.append(parent_info)
                                elif codeSystemObservationLocal == "2.16.840.1.113883.3.989.2.1.1.19":
                                    # D.10.7.2
                                    set_icsr_field(self.find(observationLocal, "value"), None, d, "d_10_7_2_text_medical_history_parent")
                        elif codeOrganizerSubjectOf2 == "2":
                            componentSmalls = self.find(organizerSubjectOf2, "component", False)
                            for componentSmallCopy in componentSmalls:
                                drug_history_parent = D_10_8_r_past_drug_history_parent(d_patient_characteristics=d)
                                substanceAdministrationSmall = self.find(componentSmallCopy, "substanceAdministration")
                                effectiveTimeSmall = self.find(substanceAdministrationSmall, "effectiveTime")
                                # D.10.8.r.4
                                set_text_icsr_field_with_null(self.find(effectiveTimeSmall, "low"), "value", drug_history_parent, "d_10_8_r_4_start_date")
                                # D.10.8.r.5
                                set_text_icsr_field_with_null(self.find(effectiveTimeSmall, "high"), "value", drug_history_parent, "d_10_8_r_5_end_date")
                                kindOfProductSmall = self.find(self.find(self.find(substanceAdministrationSmall, "consumable"), "instanceOfKind"), "kindOfProduct")
                                if drug_history_parent is not None and drug_history_parent.d_10_8_r_2a_mpid_version is not None:
                                    # D.10.8.r.2a
                                    set_icsr_field(self.find(kindOfProductSmall, "code"), "code", drug_history_parent, "d_10_8_r_2a_mpid_version")
                                    # D.10.8.r.2b
                                    set_icsr_field(self.find(kindOfProductSmall, "code"), "codeSystemVersion", drug_history_parent, "d_10_8_r_2b_mpid")
                                else:
                                    # D.10.8.r.3a
                                    set_icsr_field(self.find(kindOfProductSmall, "code"), "code", drug_history_parent, "d_10_8_r_3a_phpid_version")
                                    # D.10.8.r.3b
                                    set_icsr_field(self.find(kindOfProductSmall, "code"), "codeSystemVersion", drug_history_parent, "d_10_8_r_3b_phpid")
                                # D.10.8.r.1
                                set_icsr_field(self.find(kindOfProductSmall, "name"), None, drug_history_parent, "d_10_8_r_1_name_drug")
                                outboundRelationshipsSmall2 = self.find(substanceAdministrationSmall, "outboundRelationship2")
                                for outboundRelationshipSmall2 in outboundRelationshipsSmall2:
                                    observationOutboundRelationshipsSmall2 = self.find(outboundRelationshipSmall2, "observation")
                                    codeTmp = self.find(observationOutboundRelationshipsSmall2, "code").get("code")
                                    if codeTmp == "19":
                                        # D.10.8.r.6a
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystem", drug_history_parent, "d_10_8_r_6a_meddra_version_indication")
                                        # D.10.8.r.6b
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", drug_history_parent, "d_10_8_r_6b_indication_meddra_code")
                                    elif codeTmp == "29":
                                        # D.10.8.r.7a
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", drug_history_parent, "d_10_8_r_7a_meddra_version_reaction")
                                        # D.10.8.r.7b
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", drug_history_parent, "d_10_8_r_7b_reactions_meddra_code")
                                d.d_10_8_r_past_drug_history_parent.append(drug_history_parent)                                           
                subjectOf1 = self.find(primaryRole, "subjectOf1")
                researchStudy = self.find(subjectOf1, "researchStudy")
                # C.5.3
                set_text_icsr_field_with_null(
                    self.find(researchStudy, "id"), 
                    "extension",
                    c5, 
                    "c_5_3_sponsor_study_number"
                )

                # C.5.4
                set_icsr_field(self.find(researchStudy, "code"), "code", c5, "c_5_4_study_type_reaction")

                # C.5.2
                set_text_icsr_field_with_null(
                    self.find(researchStudy, "title"),
                    None,
                    c5,
                    "c_5_2_study_name"
                )
                authorizations = self.find(researchStudy, "authorization", False)
                for authorizationCopy in authorizations :
                    study_registration = C_5_1_r_study_registration(c_5_study_identification=c5)
                    studyRegistration = self.find(authorizationCopy, "studyRegistration")
                    # C.5.1.r.1
                    set_text_icsr_field_with_null(self.find(studyRegistration, "id"), "extension", study_registration, "c_5_1_r_1_study_registration_number")
                    # C.5.1.r.2
                    set_text_icsr_field_with_null(
                        self.find(
                            self.find(
                                self.find(
                                    self.find(studyRegistration, "author"),
                                    "territorialAuthority"
                                ),
                                "governingPlace"
                            ),
                            "code"
                        ),
                        "code",
                        study_registration,
                        "c_5_1_r_2_study_registration_country"
                    )
                    c5.c_5_1_r_study_registration.append(study_registration)
                
                subjectOf2s = self.find(primaryRole, "subjectOf2")
                ei_mapper = {}
                for subjectOf2 in subjectOf2s:
                    observation = self.find(subjectOf2, "observation")
                    organizer = self.find(subjectOf2, "organizer")
                    if observation is not None:
                        code_subjectOf2 = self.find(observation, "code")
                        code_subjectOf2_code_value = code_subjectOf2.get("code")
                        val = self.find(observation, "value")
                        if code_subjectOf2_code_value == "3":
                            # D.2.2a
                            set_icsr_field(val, "value", d, "d_2_2a_age_onset_reaction_num")
                            # D.2.2b
                            set_icsr_field(val, "unit", d, "d_2_2b_age_onset_reaction_unit")
                        elif code_subjectOf2_code_value == "16":
                            # D.2.2.1a
                            set_icsr_field(val, "value", d, "d_2_2_1a_gestation_period_reaction_foetus_num")
                            # D.2.2.1b
                            set_icsr_field(val, "unit", d, "d_2_2_1b_gestation_period_reaction_foetus_unit")
                        elif code_subjectOf2_code_value == "4":
                            # D.2.3 
                            set_icsr_field(val, "code", d, "d_2_3_patient_age_group")
                        elif code_subjectOf2_code_value == "7":
                            # D.3
                            set_icsr_field(val, "value", d, "d_3_body_weight")
                        elif code_subjectOf2_code_value == "17":
                            # D.4
                            set_icsr_field(val, "value", d, "d_4_height")
                        elif code_subjectOf2_code_value == "22":
                            # D.6
                            set_icsr_field(val, "value", d, "d_6_last_menstrual_period_date")
                        elif code_subjectOf2_code_value == "32":
                            cause_death = D_9_2_r_cause_death(d_patient_characteristics=d)
                            valueLocal = self.find(self.find(subjectOf2, "observation"), "value")
                            # D.9.2.r.1a
                            set_icsr_field(valueLocal, "codeSystemVersion", cause_death, "d_9_2_r_1a_meddra_version_cause_death")
                            # D.9.2.r.1b
                            set_icsr_field(valueLocal, "code", cause_death, "d_9_2_r_1b_cause_death_meddra_code")
                            # D.9.2.r.2
                            set_icsr_field(self.find(valueLocal, "originalText"), None, cause_death, "d_9_2_r_2_cause_death")
                            d.d_9_2_r_cause_death.append(cause_death)
                        elif code_subjectOf2_code_value == "5":
                            # D.9.3
                            set_text_icsr_field_with_null(val, "value", d, "d_9_3_autopsy")
                            outboundRelationship2Locals = self.find(observation, "outboundRelationship2", False)
                            for outboundRelationship2LocalCopy in outboundRelationship2Locals:
                                determined_cause_death = D_9_4_r_autopsy_determined_cause_death(d_patient_characteristics=d)
                                valueLocal = self.find(self.find(outboundRelationship2LocalCopy, "observation"), "value")
                                # D.9.4.r.1a
                                set_icsr_field(valueLocal, "codeSystemVersion", determined_cause_death, "d_9_4_r_1a_meddra_version_autopsy_determined_cause_death")
                                # D.9.4.r.1b
                                set_icsr_field(valueLocal, "code", determined_cause_death, "d_9_4_r_1b_autopsy_determined_cause_death_meddra_code")
                                # D.9.4.r.2
                                set_icsr_field(self.find(valueLocal, "originalText"), None, determined_cause_death, "d_9_4_r_2_autopsy_determined_cause_death")
                                d.d_9_4_r_autopsy_determined_cause_death.append(determined_cause_death)
                        elif code_subjectOf2_code_value == "29":
                            # reaction section
                            reaction = E_i_reaction_event(icsr=icsr, uuid=uuid.uuid4())
                            ei_mapper[self.find(self.find(subjectOf2, "observation"), "id").get("root")] = reaction
                            observation_new = self.find(subjectOf2, "observation")

                            effectiveTime = self.find(observation_new, "effectiveTime")
                            low = self.find(effectiveTime, "low")
                            high = self.find(effectiveTime, "high")
                            width = self.find(effectiveTime, "width")
                            comps = self.find(effectiveTime, "comp")
                            if comps is not None:
                                for comp in comps:
                                    if low is None:
                                        low = self.find(comp, "low")
                                    if high is None:
                                        high = self.find(comp, "high")
                                    if width is None:
                                        width = self.find(comp, "width")
                            # E.i.4
                            set_icsr_field(low, "value", reaction, "e_i_4_date_start_reaction")
                            # E.i.5
                            set_icsr_field(high, "value", reaction, "e_i_5_date_end_reaction")
                            # E.i.6a
                            set_icsr_field(width, "value", reaction, "e_i_6a_duration_reaction_num")
                            # E.i.6b
                            set_icsr_field(width, "unit", reaction, "e_i_6b_duration_reaction_unit")
                            value = self.find(observation_new, "value")
                            # E.i.2.1a
                            set_icsr_field(value, "codeSystemVersion", reaction, "e_i_2_1a_meddra_version_reaction")
                            # E.i.2.1b
                            set_icsr_field(value, "code", reaction, "e_i_2_1b_reaction_meddra_code")
                            originalText = self.find(value, "originalText")
                            # E.i.1.1a
                            set_icsr_field(originalText, None, reaction, "e_i_1_1a_reaction_primary_source_native_language")
                            # E.i.1.1b
                            set_icsr_field(originalText, "language", reaction, "e_i_1_1b_reaction_primary_source_language")

                            # E.i.9 Identification of the Country Where the Reaction Occurred
                            location = self.find(observation_new, "location")
                            locationCode = self.find(self.find(self.find(location, "locatedEntity"), "locatedPlace"), "code")
                            set_icsr_field(locationCode, "code", reaction, "e_i_9_identification_country_reaction")
                        
                            outboundRelationships2 = self.find(observation_new, "outboundRelationship2")
                            for outboundRelationship2 in outboundRelationships2:
                                observationOutboundRelationship = self.find(outboundRelationship2, "observation")
                                codeOutboundRelationship = self.find(observationOutboundRelationship, "code")
                                valueOutboundRelationship = self.find(observationOutboundRelationship, "value")
                                if codeOutboundRelationship == "30":
                                    # E.i.1.2
                                    set_icsr_field(valueOutboundRelationship, None, reaction, "e_i_1_2_reaction_primary_source_translation")
                                elif codeOutboundRelationship == "37":
                                    # E.i.3.1
                                    set_icsr_field(valueOutboundRelationship, "code", reaction, "e_i_3_1_term_highlighted_reporter") 
                                elif codeOutboundRelationship == "34":
                                    # E.i.3.2a
                                    set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2a_results_death") 
                                elif codeOutboundRelationship == "21":
                                    # E.i.3.2b
                                    set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2b_life_threatening") 
                                elif codeOutboundRelationship == "33":
                                    # E.i.3.2c
                                    set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2c_caused_prolonged_hospitalisation") 
                                elif codeOutboundRelationship == "35":
                                    # E.i.3.2d
                                    set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2d_disabling_incapacitating") 
                                elif codeOutboundRelationship == "12":
                                    # E.i.3.2e
                                    set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2e_congenital_anomaly_birth_defect")                    
                                elif codeOutboundRelationship == "26":
                                    # E.i.3.2f
                                    set_text_icsr_field_with_null(valueOutboundRelationship, "value", reaction, "e_i_3_2f_other_medically_important_condition")
                                elif codeOutboundRelationship == "27":
                                    # E.i.7
                                    set_icsr_field(valueOutboundRelationship, "code", reaction, "e_i_7_outcome_reaction_last_observation")
                                elif codeOutboundRelationship == "24":
                                    # E.i.8
                                    set_icsr_field(valueOutboundRelationship, "value", reaction, "e_i_8_medical_confirmation_healthcare_professional")  
                            ei.append(reaction)
                    if organizer is not None:
                        code_subjectOf2 = self.find(organizer, "code").get("code")
                        if code_subjectOf2 == "1":
                            components = self.find(organizer, "component")
                            for component in components:
                                codeTmp = self.find(self.find(component, "observation"), "code")
                                if codeTmp.get("codeSystem") == "2.16.840.1.113883.6.163":
                                    information_medical_history = D_7_1_r_structured_information_medical_history(d_patient_characteristics=d)
                                    observationOrganizer = self.find(component, "observation")
                                    codeOservationOrganizer = self.find(observationOrganizer, "code")
                                    # D.7.1.r.1a:
                                    set_icsr_field(codeOservationOrganizer, "codeSystemVersion", information_medical_history, "d_7_1_r_1a_meddra_version_medical_history")
                                    # D.7.1.r.1b
                                    set_icsr_field(codeOservationOrganizer, "code", information_medical_history, "d_7_1_r_1b_medical_history_meddra_code")
                                    # D.7.1.r.2
                                    set_text_icsr_field_with_null(self.find(self.find(observationOrganizer, "effectiveTime"), "low"), "value", information_medical_history, "d_7_1_r_2_start_date")
                                    # D.7.1.r.4
                                    set_text_icsr_field_with_null(self.find(self.find(observationOrganizer, "effectiveTime"), "high"), "value", information_medical_history, "d_7_1_r_4_end_date")
                                    for tmpOutboundRelationship2 in self.find(observationOrganizer, "outboundRelationship2"):
                                        codeTmpLocal = self.find(self.find(tmpOutboundRelationship2, "observation"), "code").get("code")
                                        valTmp = self.find(self.find(tmpOutboundRelationship2, "observation"), "value")
                                        if codeTmpLocal == "10":
                                            set_icsr_field(valTmp, None, information_medical_history, "d_7_1_r_5_comments")                                                    
                                        elif codeTmpLocal == "38":
                                            set_icsr_field(valTmp, "value", information_medical_history, "d_7_1_r_6_family_history")
                                    # D.7.1.r.3
                                    set_text_icsr_field_with_null(
                                        self.find(self.find(self.find(observationOrganizer, "inboundRelationship"), "observation"), "value"),
                                        "value",
                                        information_medical_history,
                                        "d_7_1_r_3_continuing"
                                    )
                                    d.d_7_1_r_structured_information_medical_history.append(information_medical_history)
                                elif codeTmp.get("code") == "18":
                                    # D.7.2
                                    val = self.find(self.find(component, "observation"), "value")
                                    set_text_icsr_field_with_null(val, None, d, "d_7_2_text_medical_history")
                                elif codeTmp.get("code") == "11":
                                    # D.7.3
                                    val = self.find(self.find(component, "observation"), "value")
                                    set_icsr_field(val, "value", d, "d_7_3_concomitant_therapies")
                        if code_subjectOf2 == "2":
                            components = self.find(organizer, "component", False)
                            for component_new in components:
                                drug_history = D_8_r_past_drug_history(d_patient_characteristics=d)
                                substanceAdministration = self.find(component_new, "substanceAdministration")
                                effectiveTime = self.find(substanceAdministration, "effectiveTime")
                                lowLocal = self.find(effectiveTime, "low")
                                highLocal = self.find(effectiveTime, "high")
                                # D.8.r.4
                                set_text_icsr_field_with_null(lowLocal, "value", drug_history, "d_8_r_4_start_date")
                                # D.8.r.5
                                set_text_icsr_field_with_null(highLocal, "value", drug_history, "d_8_r_5_end_date")
                                kindOfProduct = self.find(self.find(self.find(substanceAdministration, "consumable"), "instanceOfKind"), "kindOfProduct")
                                if drug_history is not None and drug_history.d_8_r_2a_mpid_version is not None:
                                    # D.8.r.2a
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_2a_mpid_version")
                                    # D.8.r.2b
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_2b_mpid")
                                else:
                                    # D.8.r.3a
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_3a_phpid_version")
                                    # D.8.r.3b
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_history, "d_8_r_3b_phpid")
                                # D.8.r.1
                                set_icsr_field(self.find(kindOfProduct, "name"), None, drug_history, "d_8_r_1_name_drug")
                                outboundRelationshipsSmall2 = self.find(substanceAdministration, "outboundRelationship2")
                                for outboundRelationshipSmall2 in outboundRelationshipsSmall2:
                                    observationOutboundRelationshipsSmall2 = self.find(outboundRelationshipSmall2, "observation")
                                    codeTmp = self.find(observationOutboundRelationshipsSmall2, "code").get("code")
                                    if codeTmp == "19":
                                        # D.8.r.6a
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", drug_history, "d_8_r_6a_meddra_version_indication")
                                        # D.8.r.6b
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", drug_history, "d_8_r_6b_indication_meddra_code")
                                    elif codeTmp == "29":
                                        # D.8.r.7a
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "codeSystemVersion", drug_history, "d_8_r_7a_meddra_version_reaction")
                                        # D.8.r.7b
                                        set_icsr_field(self.find(observationOutboundRelationshipsSmall2, "value"), "code", drug_history, "d_8_r_7b_reaction_meddra_code")
                                d.d_8_r_past_drug_history.append(drug_history)
                        elif code_subjectOf2 == "3":
                            components = self.find(organizer, "component", False)
                            for componentTmp in components:
                                tests_procedures = F_r_results_tests_procedures_investigation_patient(icsr=icsr)
                                observationTmp = self.find(componentTmp, "observation")
                                codeTmp = self.find(observationTmp, "code")
                                # F.r.2.2a
                                set_icsr_field(codeTmp, "codeSystemVersion", tests_procedures, "f_r_2_2a_meddra_version_test_name")
                                # F.r.2.2b
                                set_icsr_field(codeTmp, "code", tests_procedures, "f_r_2_2b_test_name_meddra_code")
                                # F.r.2.1
                                set_icsr_field(self.find(codeTmp, "originalText"), None, tests_procedures, "f_r_2_1_test_name")
                                # F.r.1
                                set_icsr_field(self.find(observationTmp, "effectiveTime"), "value", tests_procedures, "f_r_1_test_date")
                                valTmp = self.find(observationTmp, "value")
                                willfill = self.find(valTmp, "center")
                                if willfill is None:
                                    willfill = self.find(valTmp, "low")
                                    if willfill is None or willfill.get("nullFlavor") is None:
                                        willfill = self.find(valTmp, "high")
                                if willfill is not None:
                                    # F.r.3.2
                                    set_text_icsr_field_with_null(willfill, "value", tests_procedures, "f_r_3_2_test_result_val_qual")
                                    # F.r.3.3
                                    set_icsr_field(willfill, "unit", tests_procedures, "f_r_3_3_test_result_unit")
                                    if self.find(observationTmp, "interpretationCode") is not None:                            
                                        # F.r.3.1
                                        set_icsr_field(self.find(observationTmp, "interpretationCode"), "code", tests_procedures, "f_r_3_1_test_result_code")                                                          
                                        for referenceRange in self.find(observationTmp, "referenceRange"):
                                            interpretationCodeTmp = self.find(self.find(referenceRange, "observationRange"), "interpretationCode").get("code")
                                            valReferenceRange = self.find(self.find(referenceRange, "observationRange"), "value")
                                            if interpretationCodeTmp == "L":
                                                # F.r.4
                                                set_icsr_field(valReferenceRange, "value", tests_procedures, "f_r_4_normal_low_value")
                                            elif interpretationCodeTmp == "H":
                                                # F.r.5
                                                set_icsr_field(valReferenceRange, "value", tests_procedures, "f_r_5_normal_high_value")
                                        for iter in self.find(observationTmp, "outboundRelationship2"):
                                            interpretationCodeTmp = self.find(self.find(iter, "observation"), "code").get("code")
                                            valReferenceRange = self.find(self.find(iter, "observation"), "value")
                                            if interpretationCodeTmp == "10":
                                                # F.r.6
                                                set_icsr_field(valReferenceRange, None, tests_procedures, "f_r_6_comments")
                                            elif interpretationCodeTmp == "25":
                                                # F.r.7
                                                set_icsr_field(valReferenceRange, "value", tests_procedures, "f_r_7_more_information_available")
                                else:
                                    # F.r.3.4
                                    set_icsr_field(valTmp, None, tests_procedures, "f_r_3_4_result_unstructured_data")
                                fr.append(tests_procedures)

                        elif code_subjectOf2 == "4":
                            # drugInformation
                            components = self.find(organizer, "component", False)
                            for component_new in components:
                                drug_info = G_k_drug_information(icsr=icsr)
                                substanceAdministration = self.find(component_new, "substanceAdministration")                                
                                instanceOfKind = self.find(self.find(substanceAdministration, "consumable"), "instanceOfKind")
                                kindOfProduct = self.find(instanceOfKind, "kindOfProduct")
                                if drug_info is not None and drug_info.g_k_2_1_1a_mpid_version is not None:
                                    # G.k.2.1.1a
                                    set_icsr_field(self.find(kindOfProduct, "code"), "codeSystemVersion", drug_info, "g_k_2_1_1a_mpid_version")
                                    # G.k.2.1.1b
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_info, "g_k_2_1_1b_mpid")
                                else:
                                    # G.k.2.1.2a
                                    set_icsr_field(self.find(kindOfProduct, "code"), "codeSystemVersion", drug_info, "g_k_2_1_2a_phpid_version")
                                    # G.k.2.1.2b
                                    set_icsr_field(self.find(kindOfProduct, "code"), "code", drug_info, "g_k_2_1_2b_phpid")
                                # G.k.2.2
                                set_icsr_field(self.find(kindOfProduct, "name"), None, drug_info, "g_k_2_2_medicinal_product_name_primary_source")
                                approval = self.find(self.find(self.find(kindOfProduct, "asManufacturedProduct"), "subjectOf"), "approval")
                                # G.k.3.1
                                set_icsr_field(self.find(approval, "id"), "extension", drug_info, "g_k_3_1_authorisation_application_number")
                                # G.k.3.3
                                set_icsr_field(
                                    self.find(self.find(self.find(self.find(approval, "holder"), "role"), "playingOrganization"), "name"), 
                                    None, 
                                    drug_info, 
                                    "g_k_3_3_name_holder_applicant"
                                )
                                # G.k.3.2
                                set_icsr_field(
                                    self.find(self.find(self.find(self.find(approval, "author"), "territorialAuthority"), "territory"), "code"), 
                                    "code", 
                                    drug_info, 
                                    "g_k_3_2_country_authorisation_application"
                                )

                                ingredients = self.find(kindOfProduct, "ingredient", False)
                                for ingredient_new in ingredients:
                                    substance = G_k_2_3_r_substance_id_strength(g_k_drug_information=drug_info)
                                    numerator = self.find(self.find(ingredient_new, "quantity"), "numerator")
                                    # G.k.2.3.r.3a
                                    set_icsr_field(numerator, "value", substance, "g_k_2_3_r_3a_strength_num")
                                    # G.k.2.3.r.3b
                                    set_icsr_field(numerator, "unit", substance, "g_k_2_3_r_3b_strength_unit")
                                    # G.k.2.3.r.2a
                                    set_icsr_field(self.find(self.find(ingredient_new, "ingredientSubstance"), "code"), "codeSystemVersion", substance, "g_k_2_3_r_2a_substance_termid_version")
                                    # G.k.2.3.r.2b
                                    set_icsr_field(self.find(self.find(ingredient_new, "ingredientSubstance"), "code"), "code", substance, "g_k_2_3_r_2b_substance_termid")
                                    # G.k.2.3.r.1
                                    set_icsr_field(self.find(self.find(ingredient_new, "ingredientSubstance"), "name"), None, substance, "g_k_2_3_r_1_substance_name")
                                    drug_info.g_k_2_3_r_substance_id_strength.append(substance)
                                subjectOfIngredient = self.find(instanceOfKind, "subjectOf")
                                performer = self.find(self.find(subjectOfIngredient, "productEvent"), "performer")
                                country = self.find(self.find(self.find(self.find(performer, "assignedEntity"), "representedOrganization"), "addr"), "country")
                                # G.k.2.4
                                set_icsr_field(country, None, drug_info, "g_k_2_4_identification_country_drug_obtained")
                                outboundRelationship1s = self.find(substanceAdministration, "outboundRelationship1")
                                # if icsr.id is None:
                                #     icsr, _ = self.domain_service.create(icsr)
                                for i, outboundRelationship1 in enumerate(outboundRelationship1s, start=0):
                                    ei_obj = ei_mapper[self.find(self.find(outboundRelationship1, "actReference"), "id").get("root")]
                                    reaction_matrix = G_k_9_i_drug_reaction_matrix(g_k_9_i_1_reaction_assessed=ei_obj.uuid)
                                    pauseQuantity = self.find(outboundRelationship1, "pauseQuantity")
                                    # no other identifiers
                                    if pauseQuantity.get("unit") == "G.k.9.i.3.1b":
                                        # G.k.9.i.3.1a
                                        set_icsr_field(pauseQuantity, "value", reaction_matrix, "g_k_9_i_3_1a_interval_drug_administration_reaction_num")
                                        # G.k.9.i.3.1b
                                        set_icsr_field(pauseQuantity, "unit", reaction_matrix, "g_k_9_i_3_1b_interval_drug_administration_reaction_unit")
                                    elif pauseQuantity.get("unit") == "G.k.9.i.3.1b":
                                        # G.k.9.i.3.2a
                                        set_icsr_field(pauseQuantity, "value", reaction_matrix, "g_k_9_i_3_2a_interval_last_dose_drug_reaction_num")
                                        # G.k.9.i.3.2b
                                        set_icsr_field(pauseQuantity, "unit", reaction_matrix, "g_k_9_i_3_2b_interval_last_dose_drug_reaction_unit")
                                    drug_info.g_k_9_i_drug_reaction_matrix.append(reaction_matrix)

                                outboundRelationship2s = self.find(substanceAdministration, "outboundRelationship2")
                                g_k_9_i_ind = 0
                                for outboundRelationship2 in outboundRelationship2s:
                                    typeCode = outboundRelationship2.get("typeCode")
                                    if typeCode == "PERT" or typeCode == "SUMM" or typeCode == "REFR":
                                        observationTmp = self.find(outboundRelationship2, "observation")
                                        observationCode = self.find(observationTmp, "code").get("code")
                                        valTmp = self.find(observationTmp, "value")
                                        if observationCode == "6":
                                            # G.k.2.5
                                            set_icsr_field(valTmp, "value", drug_info, "g_k_2_5_investigational_product_blinded")
                                        elif observationCode == "14":
                                            # G.k.5a
                                            set_icsr_field(valTmp, "value", drug_info, "g_k_5a_cumulative_dose_first_reaction_num")
                                            # G.k.5b
                                            set_icsr_field(valTmp, "unit", drug_info, "g_k_5b_cumulative_dose_first_reaction_unit")
                                        elif observationCode == "16":
                                            # G.k.6a
                                            set_icsr_field(valTmp, "value", drug_info, "g_k_6a_gestation_period_exposure_num")
                                            # G.k.6b
                                            set_icsr_field(valTmp, "unit", drug_info, "g_k_6b_gestation_period_exposure_unit")
                                        elif observationCode == "31":
                                            react_matrix = drug_info.g_k_9_i_drug_reaction_matrix[g_k_9_i_ind]
                                            observationOutboundRelationship2 = self.find(outboundRelationship2, "observation")
                                            # G.k.9.i.4
                                            set_icsr_field(self.find(observationOutboundRelationship2, "value"), "code", react_matrix, "g_k_9_i_4_reaction_recur_readministration")
                                            g_k_9_i_ind += 1
                                        elif observationCode == "9":
                                            additional_information = G_k_10_r_additional_information_drug(g_k_drug_information=drug_info)
                                            observationTmp = self.find(outboundRelationship2, "observation")
                                            observationCode = self.find(observationTmp, "code").get("code")
                                            valTmp = self.find(observationTmp, "value")
                                            # G.k.10.r
                                            set_icsr_field(valTmp, "code", additional_information, "g_k_10_r_additional_information_drug")
                                            drug_info.g_k_10_r_additional_information_drug.append(additional_information)
                                        elif observationCode == "2":
                                            # G.k.11
                                            set_icsr_field(valTmp, None, drug_info, "g_k_11_additional_information_drug")
                                    elif typeCode == "COMP":
                                        dosage_information = G_k_4_r_dosage_information(g_k_drug_information=drug_info)
                                        substanceAdministration_new = self.find(outboundRelationship2, "substanceAdministration")
                                        # G.k.4.r.8
                                        set_icsr_field(self.find(substanceAdministration_new, "text"), None, dosage_information, "g_k_4_r_8_dosage_text")
                                        effectiveTime = self.find(substanceAdministration_new, "effectiveTime")
                                        comps = self.find(effectiveTime, "comp", False)
                                        if comps is None:
                                            period = self.find(effectiveTime, "period")
                                            if period is not None:
                                                # G.k.4.r.2
                                                set_icsr_field(period, "value", dosage_information, "g_k_4_r_2_number_units_interval")
                                                # G.k.4.r.3
                                                set_icsr_field(period, "unit", dosage_information, "g_k_4_r_3_definition_interval_unit")
                                        else:
                                            for comp in comps:
                                                period = self.find(comp, "period")
                                                low = self.find(comp, "low")
                                                high = self.find(comp, "high")
                                                width = self.find(comp, "width")
                                                if period is not None:
                                                    # G.k.4.r.2
                                                    set_icsr_field(period, "value", dosage_information, "g_k_4_r_2_number_units_interval")
                                                    # G.k.4.r.3
                                                    set_icsr_field(period, "unit", dosage_information, "g_k_4_r_3_definition_interval_unit")
                                                elif low is not None:
                                                    # G.k.4.r.4
                                                    set_text_icsr_field_with_null(low, "value", dosage_information, "g_k_4_r_4_date_time_drug")
                                                    # G.k.4.r.5
                                                    set_text_icsr_field_with_null(high, "value", dosage_information, "g_k_4_r_5_date_time_last_administration")
                                                elif width is not None:
                                                    # G.k.4.r.6a
                                                    set_icsr_field(width, "value", dosage_information, "g_k_4_r_6a_duration_drug_administration_num")
                                                    # G.k.4.r.6b
                                                    set_icsr_field(width, "unit", dosage_information, "g_k_4_r_6b_duration_drug_administration_unit")
                                            
                                        # G.k.4.r.10.2a
                                        set_icsr_field(self.find(substanceAdministration_new, "routeCode"), "codeSystemVersion", dosage_information, "g_k_4_r_10_2a_route_administration_termid_version")
                                        # G.k.4.r.10.2b
                                        set_icsr_field(self.find(substanceAdministration_new, "routeCode"), "code", dosage_information, "g_k_4_r_10_2b_route_administration_termid")
                                        # G.k.4.r.10.1
                                        set_text_icsr_field_with_null(self.find(self.find(substanceAdministration_new, "routeCode"), "originalText"), None, dosage_information, "g_k_4_r_10_1_route_administration")
                                                                                    
                                        doseQuantity = self.find(substanceAdministration_new, "doseQuantity")
                                        # G.k.4.r.1a
                                        set_icsr_field(doseQuantity, "value", dosage_information, "g_k_4_r_1a_dose_num")
                                        # G.k.4.r.1b
                                        set_icsr_field(doseQuantity, "unit", dosage_information, "g_k_4_r_1b_dose_unit")
                                        instanceOfKind = self.find(self.find(substanceAdministration_new, "consumable"), "instanceOfKind")
                                        productInstanceInstance = self.find(instanceOfKind, "productInstanceInstance")
                                        # G.k.4.r.7
                                        set_icsr_field(self.find(productInstanceInstance, "lotNumberText"), None, dosage_information, "g_k_4_r_7_batch_lot_number")
                                        kindOfProduct = self.find(instanceOfKind, "kindOfProduct")
                                        formCode = self.find(kindOfProduct, "formCode")
                                        # G.k.4.r.9.2a
                                        set_icsr_field(formCode, "codeSystemVersion", dosage_information, "g_k_4_r_9_2a_pharmaceutical_dose_form_termid_version")
                                        # G.k.4.r.9.2b
                                        set_icsr_field(formCode, "codeSystem", dosage_information, "g_k_4_r_9_2b_pharmaceutical_dose_form_termid")
                                        # G.k.4.r.9.1
                                        set_text_icsr_field_with_null(self.find(formCode, "originalText"), None, dosage_information, "g_k_4_r_9_1_pharmaceutical_dose_form")
                                        valueInboundRelationshipTmp = self.find(self.find(self.find(substanceAdministration_new, "inboundRelationship"), "observation"), "value")
                                        # G.k.4.r.11.2a
                                        set_icsr_field(valueInboundRelationshipTmp, "codeSystemVersion", dosage_information, "g_k_4_r_11_2a_parent_route_administration_termid_version")
                                        # G.k.4.r.11.2b
                                        set_icsr_field(formCode, "code", dosage_information, "g_k_4_r_11_2b_parent_route_administration_termid")
                                        # G.k.4.r.11.1
                                        set_text_icsr_field_with_null(self.find(valueInboundRelationshipTmp, "originalText"), None, dosage_information, "g_k_4_r_11_1_parent_route_administration")
                                            
                                        drug_info.g_k_4_r_dosage_information.append(dosage_information)
                                        
                                inboundRelationships = self.find(substanceAdministration, "inboundRelationship")
                                for inboundRelationship in inboundRelationships:
                                    typeCode = inboundRelationship.get("typeCode")
                                    if typeCode == "RSON":
                                        observationInboundRelationship = self.find(inboundRelationship, "observation")
                                        vals = self.find(observationInboundRelationship, "value", False)
                                        for val_new in vals:
                                            indication = G_k_7_r_indication_use_case(g_k_drug_information=drug_info)
                                            # G.k.7.r.2a
                                            set_icsr_field(val_new, "codeSystemVersion", indication, "g_k_7_r_2a_meddra_version_indication")
                                            # G.k.7.r.2b
                                            set_icsr_field(val_new, "code", indication, "g_k_7_r_2b_indication_meddra_code")
                                            # G.k.7.r.1
                                            set_text_icsr_field_with_null(self.find(val_new, "originalText"), None, indication, "g_k_7_r_1_indication_primary_source")
                                            drug_info.g_k_7_r_indication_use_case.append(indication)
                                    elif typeCode == "CAUS":
                                        # G.k.8
                                        set_icsr_field(
                                            self.find(self.find(inboundRelationship, "act"), "code"),
                                            "code",
                                            drug_info,
                                            "g_k_8_action_taken_drug"
                                        )
                                gk.append(drug_info)
                # end of subject1
        
                components = self.find(adverseEventAssessment, "component")
                for component in components:
                    causalityAssessment = self.find(component, "causalityAssessment")
                    codeCausalityAssessment = self.find(causalityAssessment, "code").get("code")
                    for k, drug_info in enumerate(gk, start=1):
                        if codeCausalityAssessment == "20":
                            causalityAssessment_new = self.find(component, "causalityAssessment")
                            # G.k.1
                            set_icsr_field(
                                self.find(causalityAssessment_new, "value"), 
                                "code", 
                                drug_info, 
                                "g_k_1_characterisation_drug_role"
                            )
                        elif codeCausalityAssessment == "39":
                            for i, react_matrix in enumerate(drug_info.g_k_9_i_drug_reaction_matrix, start=1):
                                relatedness_drug_reaction = G_k_9_i_2_r_assessment_relatedness_drug_reaction(g_k_9_i_drug_reaction_matrix=react_matrix)
                                causalityAssessment_new = self.find(component, "causalityAssessment")
                                # G.k.9.i.2.r.3
                                set_icsr_field(self.find(causalityAssessment_new, "value"), None, relatedness_drug_reaction, "g_k_9_i_2_r_3_result_assessment")
                                # G.k.9.i.2.r.2
                                set_icsr_field(
                                    self.find(self.find(causalityAssessment_new, "methodCode"), "originalText"),
                                    None, 
                                    relatedness_drug_reaction,
                                    "g_k_9_i_2_r_2_method_assessment"
                                )
                                # G.k.9.i.2.r.1
                                set_icsr_field(
                                    self.find(self.find(
                                        self.find(self.find(causalityAssessment_new, "author"), "assignedEntity"),
                                        "code"
                                    ), "originalText"),
                                    None,
                                    relatedness_drug_reaction,
                                    "g_k_9_i_2_r_1_source_assessment"
                                )                                           
                                react_matrix.g_k_9_i_2_r_assessment_relatedness_drug_reaction.append(relatedness_drug_reaction)
                
                components1 = self.find(adverseEventAssessment, "component1")
                for component1 in components1:
                    codeTmp = self.find(self.find(component1, "observationEvent"), "code").get("code")
                    if codeTmp == "10":
                        codeLocal = self.find(self.find(self.find(self.find(component1, "observationEvent"), "author"), "assignedEntity"), "code").get("code")
                        if codeLocal == "3":
                            # H.2
                            set_icsr_field(
                                self.find(self.find(component1, "observationEvent"), "value"),
                                None,
                                h,
                                "h_2_reporter_comments"
                            )
                        elif codeLocal == "1":
                            # H.4
                            set_icsr_field(
                                self.find(self.find(component1, "observationEvent"), "value"),
                                None,
                                h,
                                "h_4_sender_comments"
                            )
                    elif codeTmp == "15":
                        diagnosis_meddra_code = H_3_r_sender_diagnosis_meddra_code(h_narrative_case_summary=h)
                        valTmp = self.find(self.find(component1, "observationEvent"), "value")
                        # H.3.r.1a
                        set_icsr_field(valTmp, "codeSystemVersion", diagnosis_meddra_code, "h_3_r_1a_meddra_version_sender_diagnosis")
                        # H.3.r.1b
                        set_icsr_field(valTmp, "code", diagnosis_meddra_code, "h_3_r_1b_sender_diagnosis_meddra_code")
                        h.h_3_r_sender_diagnosis_meddra_code.append(diagnosis_meddra_code)
                # end of adverseEventAssessment

            elif observationEvent is not None:
                codeObservationEvent = self.find(observationEvent, "code").get("code")
                if codeObservationEvent == "1":
                    # C.1.6.1
                    set_icsr_field(self.find(observationEvent, "value"), "value", c1, "c_1_6_1_additional_documents_available")
                if codeObservationEvent == "23":
                    # C.1.7
                    set_text_icsr_field_with_null(self.find(observationEvent, "value"), "value", c1, "c_1_7_fulfil_local_criteria_expedited_report")
                if codeObservationEvent == "36":
                    case_summary = H_5_r_case_summary_reporter_comments_native_language(h_narrative_case_summary=h)
                    observationEvent_new = self.find(component_big, "observationEvent")
                    val = self.find(observationEvent_new, "value")
                    # H.5.r.1a
                    set_icsr_field(val, None, case_summary, "h_5_r_1a_case_summary_reporter_comments_text")
                    # H.5.r.1b
                    set_icsr_field(val, "language", case_summary, "h_5_r_1b_case_summary_reporter_comments_language")                            
                    h.h_5_r_case_summary_reporter_comments_native_language.append(case_summary)
            
        outboundRelationships = self.find(investigationEvent, 'outboundRelationship')
        for outboundRelationship in outboundRelationships:
            relatedInvestigation = self.find(outboundRelationship, "relatedInvestigation")
            codeRelatedInvestigation = self.find(relatedInvestigation, "code")
            if codeRelatedInvestigation.get("code") == "1":
                assignedEntitySmall = self.find(self.find(self.find(self.find(relatedInvestigation, "subjectOf2"), "controlActEvent"), "author"), "assignedEntity")
                # C.1.8.2
                set_icsr_field(self.find(assignedEntitySmall, "code"), "code", c1, "c_1_8_2_first_sender")
            elif codeRelatedInvestigation.get("nullFlavor") == "NA":
                for identification_number_report_linked in c1.c_1_10_r_identification_number_report_linked:
                    identification_number_report_linked = C_1_10_r_identification_number_report_linked(c_1_identification_case_safety_report=c1)
                    idSmall = self.find(self.find(self.find(self.find(outboundRelationship, "relatedInvestigation"), "subjectOf2"), "controlActEvent"), "id")
                    # C.1.10.r
                    set_icsr_field(idSmall, "extension", identification_number_report_linked, "c_1_10_r_identification_number_report_linked")
                    c1.c_1_10_r_identification_number_report_linked.append(identification_number_report_linked)
            elif codeRelatedInvestigation.get("code") == "2":
                primary_source = C_2_r_primary_source_information(icsr=icsr)
                # C.2.r.5
                set_icsr_field(self.find(outboundRelationship, "priorityNumber"), "value", primary_source, "c_2_r_5_primary_source_regulatory_purposes")
                assignedEntitySmall = self.find(self.find(self.find(self.find(
                    self.find(outboundRelationship, "relatedInvestigation"),
                    "subjectOf2"),
                    "controlActEvent"),
                    "author"),
                    "assignedEntity"
                )
                addr = self.find(assignedEntitySmall, "addr")
                # C.2.r.2.3
                set_text_icsr_field_with_null(self.find(addr, "streetAddressLine"), None, primary_source, "c_2_r_2_3_reporter_street")
                # C.2.r.2.4
                set_text_icsr_field_with_null(self.find(addr, "city"), None, primary_source, "c_2_r_2_4_reporter_city")
                # C.2.r.2.5
                set_text_icsr_field_with_null(self.find(addr, "state"), None, primary_source, "c_2_r_2_5_reporter_state_province")
                # C.2.r.2.6
                set_text_icsr_field_with_null(self.find(addr, "postalCode"), None, primary_source, "c_2_r_2_6_reporter_postcode")
                # C.2.r.2.7
                set_text_icsr_field_with_null(self.find(assignedEntitySmall, "telecom"), "value", primary_source, "c_2_r_2_7_reporter_telephone", get_value=lambda x: x[4:])
                assignedPerson = self.find(assignedEntitySmall, "assignedPerson")
                name = self.find(assignedPerson, "name")
                # C.2.r.1.1
                set_text_icsr_field_with_null(self.find(name, "prefix"), None, primary_source, "c_2_r_1_1_reporter_title")
                givens = self.find(name, "given")
                for given, field in zip(givens, ["c_2_r_1_2_reporter_given_name", "c_2_r_1_3_reporter_middle_name"]):
                    # C.2.r.1.2 / C.2.r.1.3
                    set_icsr_field(given, None, primary_source, field)
                # C.2.r.1.4
                set_text_icsr_field_with_null(self.find(name, "family"), None, primary_source, "c_2_r_1_4_reporter_family_name")
                # C.2.r.4
                set_text_icsr_field_with_null(
                    self.find(self.find(assignedPerson, "asQualifiedEntity"), "code"),
                    "code",
                    primary_source,
                    "c_2_r_4_qualification"
                )
                # C.2.r.3
                set_text_icsr_field_with_null(
                    self.find(self.find(self.find(assignedPerson, "asLocatedEntity"), "location"), "code"),
                    "code",
                    primary_source,
                    "c_2_r_3_reporter_country_code"
                )
                representedOrganization = self.find(assignedEntitySmall, "representedOrganization")
                # C.2.r.2.2
                set_text_icsr_field_with_null(self.find(representedOrganization, "name"), None, primary_source, "c_2_r_2_2_reporter_department")
                # C.2.r.2.1
                set_text_icsr_field_with_null(
                    self.find(self.find(self.find(representedOrganization, "assignedEntity"), "representedOrganization"), "name"),
                    None, 
                    primary_source,
                    "c_2_r_2_1_reporter_organisation"
                )
                c2.append(primary_source)                          
        
        subjectOf1s = self.find(investigationEvent, 'subjectOf1')
        for subjectOf1 in subjectOf1s:
            controlActEventLocall = self.find(subjectOf1, "controlActEvent")
            idControlActEventLocall = self.find(controlActEventLocall, "id")
            author = self.find(controlActEventLocall, "author")
            if idControlActEventLocall is not None:
                documents_held_sender = C_1_9_1_r_source_case_id(c_1_identification_case_safety_report=c1)
                idControlActEventLocallCopy = self.find(self.find(subjectOf1, "controlActEvent"), "id")
                # C.1.9.1.r.1
                set_icsr_field(idControlActEventLocallCopy, "assigningAuthorityName", documents_held_sender, "c_1_9_1_r_1_source_case_id")
                # C.1.9.1.r.2
                set_icsr_field(idControlActEventLocallCopy, "extension", documents_held_sender, "c_1_9_1_r_2_case_id")
                c1.c_1_9_1_r_source_case_id.append(documents_held_sender)
            elif author is not None:
                assignedEntitySmall = self.find(author, "assignedEntity")
                # C.3.1
                set_icsr_field(self.find(assignedEntitySmall, "code"), "code", c3, "c_3_1_sender_type")
                addr = self.find(assignedEntitySmall, "addr")
                # C.3.4.1
                set_icsr_field(self.find(addr, "streetAddressLine"), None, c3, "c_3_4_1_sender_street_address")
                # C.3.4.2
                set_icsr_field(self.find(addr, "city"), None, c3, "c_3_4_2_sender_city")
                # C.3.4.3
                set_icsr_field(self.find(addr, "state"), None, c3, "c_3_4_3_sender_state_province")
                # C.3.4.4
                set_icsr_field(self.find(addr, "postalCode"), None, c3, "c_3_4_4_sender_postcode")
                telecoms = self.find(assignedEntitySmall, "telecom")
                for telecom, pair in zip(telecoms, [
                    ("c_3_4_6_sender_telephone", "tel"),
                    ("c_3_4_7_sender_fax", "fax"),
                    ("c_3_4_8_sender_email", "mailto")
                ]):
                    field, prefix = pair
                    # C.3.4.6 / C.3.4.7 / C.3.4.8
                    set_icsr_field(telecom, "value", c3, field, get_value=lambda x: x[len(prefix) + 1:])
                assignedPerson = self.find(assignedEntitySmall, "assignedPerson")
                name = self.find(assignedPerson, "name")
                # C.3.3.2
                set_icsr_field(self.find(name, "prefix"), None, c3, "c_3_3_2_sender_title")
                givens = self.find(name, "given")
                for given, field in zip(givens, ["c_3_3_3_sender_given_name", "c_3_3_4_sender_middle_name"]):
                    # C.3.3.3 / C.3.3.4
                    set_icsr_field(given, None, c3, field)
                # C.3.3.5
                set_icsr_field(self.find(name, "family"), None, c3, "c_3_3_5_sender_family_name")
                # 3.4.5
                set_icsr_field(
                    self.find(self.find(self.find(assignedPerson, "asLocatedEntity"), "location"), "code"),
                    "code",
                    c3,
                    "c_3_4_5_sender_country_code"
                )
                representedOrganization = self.find(assignedEntitySmall, "representedOrganization")
                # C.3.3.1
                set_icsr_field(self.find(representedOrganization, "name"), None, c3, "c_3_3_1_sender_department")
                # C.3.2
                set_icsr_field(
                    self.find(self.find(self.find(representedOrganization, "assignedEntity"), "representedOrganization"), "name"),
                    None, 
                    c3,
                    "c_3_2_sender_organisation"
                )

        vars = self.find(investigationEvent, 'subjectOf2')
        for var in vars:
            var = self.find(var, 'investigationCharacteristic')
            code = self.find(var, "code").get("code")
            tmp_var = self.find(var,'value')
            if code == "1":
                # C.1.3
                set_icsr_field(tmp_var, "code", c1, "c_1_3_type_report")
            elif code == "2":
                # C.1.9.1
                set_text_icsr_field_with_null(tmp_var, None, c1, "c_1_9_1_other_case_ids_previous_transmissions")
            elif code == "3":
                # C.1.11.1
                set_icsr_field(tmp_var, "code", c1, "c_1_11_1_report_nullification_amendment")
            elif code == "4":
                # C.1.11.2
                set_text_icsr_field_with_null(self.find(tmp_var, "originalText"), None, c1, "c_1_11_2_reason_nullification_amendment")
        
        return icsr                   
