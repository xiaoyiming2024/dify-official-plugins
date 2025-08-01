import time
from typing import Optional
import numpy as np
import requests
from dify_plugin.entities.model import (
    AIModelEntity,
    EmbeddingInputType,
    FetchFrom,
    I18nObject,
    ModelType,
    PriceType,
)
from dify_plugin.entities.model.text_embedding import (
    EmbeddingUsage,
    TextEmbeddingResult,
)
from dify_plugin.errors.model import CredentialsValidateFailedError
from dify_plugin.interfaces.model.text_embedding_model import TextEmbeddingModel
from huggingface_hub import HfApi, InferenceClient
from models.common import _CommonHuggingfaceHub

HUGGINGFACE_ENDPOINT_API = "https://api.endpoints.huggingface.cloud/v2/endpoint/"


class HuggingfaceHubTextEmbeddingModel(_CommonHuggingfaceHub, TextEmbeddingModel):
    def _invoke(
        self,
        model: str,
        credentials: dict,
        texts: list[str],
        user: Optional[str] = None,
        input_type: EmbeddingInputType = EmbeddingInputType.DOCUMENT,
    ) -> TextEmbeddingResult:
        """
        Invoke text embedding model

        :param model: model name
        :param credentials: model credentials
        :param texts: texts to embed
        :param user: unique user id
        :param input_type: input type
        :return: embeddings result
        """
        client = InferenceClient(token=credentials["huggingfacehub_api_token"])
        execute_model = model
        if credentials["huggingfacehub_api_type"] == "inference_endpoints":
            execute_model = credentials["huggingfacehub_endpoint_url"]
        output = client.feature_extraction(
            text=texts,
            model=execute_model,
        )

        tokens = self.get_num_tokens(model, credentials, texts)
        usage = self._calc_response_usage(model, credentials, tokens)
        return TextEmbeddingResult(
            embeddings=self._mean_pooling(output), usage=usage, model=model
        )

    def get_num_tokens(self, model: str, credentials: dict, texts: list[str]) -> list[int]:
        tokens = []
        for text in texts:
            tokens.append(self._get_num_tokens_by_gpt2(text))
        return tokens

    def validate_credentials(self, model: str, credentials: dict) -> None:
        try:
            if "huggingfacehub_api_type" not in credentials:
                raise CredentialsValidateFailedError(
                    "Huggingface Hub Endpoint Type must be provided."
                )
            if "huggingfacehub_api_token" not in credentials:
                raise CredentialsValidateFailedError(
                    "Huggingface Hub API Token must be provided."
                )
            if credentials["huggingfacehub_api_type"] == "inference_endpoints":
                if "huggingface_namespace" not in credentials:
                    raise CredentialsValidateFailedError(
                        "Huggingface Hub User Name / Organization Name must be provided."
                    )
                if "huggingfacehub_endpoint_url" not in credentials:
                    raise CredentialsValidateFailedError(
                        "Huggingface Hub Endpoint URL must be provided."
                    )
                if "task_type" not in credentials:
                    raise CredentialsValidateFailedError(
                        "Huggingface Hub Task Type must be provided."
                    )
                if credentials["task_type"] != "feature-extraction":
                    raise CredentialsValidateFailedError(
                        "Huggingface Hub Task Type is invalid."
                    )
                self._check_endpoint_url_model_repository_name(credentials, model)
                model = credentials["huggingfacehub_endpoint_url"]
            elif credentials["huggingfacehub_api_type"] == "hosted_inference_api":
                self._check_hosted_model_task_type(
                    credentials["huggingfacehub_api_token"], model
                )
            else:
                raise CredentialsValidateFailedError(
                    "Huggingface Hub Endpoint Type is invalid."
                )
            client = InferenceClient(token=credentials["huggingfacehub_api_token"])
            client.feature_extraction(text="hello world", model=model)
        except Exception as ex:
            raise CredentialsValidateFailedError(str(ex))

    def get_customizable_model_schema(
        self, model: str, credentials: dict
    ) -> Optional[AIModelEntity]:
        entity = AIModelEntity(
            model=model,
            label=I18nObject(en_US=model),
            fetch_from=FetchFrom.CUSTOMIZABLE_MODEL,
            model_type=ModelType.TEXT_EMBEDDING,
            model_properties={"context_size": 10000, "max_chunks": 1},
        )
        return entity

    @staticmethod
    def _mean_pooling(embeddings: list) -> list[list[float]]:
        if not isinstance(embeddings[0][0], list):
            return embeddings
        sentence_embeddings = [
            np.mean(embedding[0], axis=0).tolist() for embedding in embeddings
        ]
        return sentence_embeddings

    @staticmethod
    def _check_hosted_model_task_type(
        huggingfacehub_api_token: str, model_name: str
    ) -> None:
        hf_api = HfApi(token=huggingfacehub_api_token)
        model_info = hf_api.model_info(repo_id=model_name)
        try:
            if not model_info:
                raise ValueError(f"Model {model_name} not found.")
            if "inference" in model_info.cardData and (
                not model_info.cardData["inference"]
            ):
                raise ValueError(
                    f"Inference API has been turned off for this model {model_name}."
                )
            valid_tasks = "feature-extraction"
            if model_info.pipeline_tag not in valid_tasks:
                raise ValueError(
                    f"Model {model_name} is not a valid task, must be one of {valid_tasks}."
                )
        except Exception as e:
            raise CredentialsValidateFailedError(f"{str(e)}")

    def _calc_response_usage(
        self, model: str, credentials: dict, tokens: int
    ) -> EmbeddingUsage:
        input_price_info = self.get_price(
            model=model,
            credentials=credentials,
            price_type=PriceType.INPUT,
            tokens=tokens,
        )
        usage = EmbeddingUsage(
            tokens=tokens[0],
            total_tokens=tokens[0],
            unit_price=input_price_info.unit_price,
            price_unit=input_price_info.unit,
            total_price=input_price_info.total_amount,
            currency=input_price_info.currency,
            latency=time.perf_counter() - self.started_at,
        )
        return usage

    @staticmethod
    def _check_endpoint_url_model_repository_name(credentials: dict, model_name: str):
        try:
            url = f"{HUGGINGFACE_ENDPOINT_API}{credentials['huggingface_namespace']}"
            headers = {
                "Authorization": f"Bearer {credentials['huggingfacehub_api_token']}",
                "Content-Type": "application/json",
            }
            response = requests.get(url=url, headers=headers)
            if response.status_code != 200:
                raise ValueError("User Name or Organization Name is invalid.")
            model_repository_name = ""
            for item in response.json().get("items", []):
                if (
                    item.get("status", {}).get("url")
                    == credentials["huggingfacehub_endpoint_url"]
                ):
                    model_repository_name = item.get("model", {}).get("repository")
                    break
            if model_repository_name != model_name:
                raise ValueError(
                    f"Model Name {model_name} is invalid. Please check it on the inference endpoints console."
                )
        except Exception as e:
            raise ValueError(str(e))
